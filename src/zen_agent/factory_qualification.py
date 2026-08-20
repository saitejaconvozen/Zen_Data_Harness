from __future__ import annotations

from dataclasses import dataclass
import json
from math import sqrt
from pathlib import Path
import sqlite3
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class ConfigurationDecision:
    configuration_key: str
    status: str
    registered: int
    audited: int
    passed: int
    failed: int
    quarantined: int
    critical_failures: int
    pass_rate: float
    wilson_lower_95: float
    reason: str


def wilson_lower(successes: int, trials: int, z: float = 1.959963984540054) -> float:
    if trials <= 0:
        return 0.0
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    margin = z * sqrt((proportion * (1 - proportion) + z * z / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denominator)


class FactoryQualificationStore:
    """Aggregate conversation audits into immutable agent-configuration decisions."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS factory_configuration (
              run_id TEXT NOT NULL, configuration_key TEXT NOT NULL,
              agent_id TEXT NOT NULL, agent_version TEXT,
              system_prompt_sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
              decision_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(run_id, configuration_key)
            );
            CREATE TABLE IF NOT EXISTS factory_configuration_sample (
              run_id TEXT NOT NULL, configuration_key TEXT NOT NULL,
              source_content_sha256 TEXT NOT NULL, packet_id TEXT NOT NULL,
              packet_batch TEXT NOT NULL, packet_index INTEGER NOT NULL,
              verdict TEXT, critical_failures INTEGER, decision_sha256 TEXT,
              promoted INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(run_id, source_content_sha256),
              FOREIGN KEY(run_id, configuration_key)
                REFERENCES factory_configuration(run_id, configuration_key)
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def configuration_key(agent_id: str, agent_version: str | None, prompt_sha256: str) -> str:
        import hashlib
        payload = json.dumps([agent_id, agent_version, prompt_sha256], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def register_packet(self, run_id: str, packet: dict[str, Any], packet_batch: str, packet_index: int) -> str:
        source = packet["source"]
        key = self.configuration_key(
            source["agent_id"], source.get("agent_version"), source["system_prompt_sha256"]
        )
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO factory_configuration
                (run_id,configuration_key,agent_id,agent_version,system_prompt_sha256,status,created_at,updated_at)
                VALUES (?,?,?,?,?,'PENDING',?,?)""",
                (run_id, key, source["agent_id"], source.get("agent_version"), source["system_prompt_sha256"], now, now),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO factory_configuration_sample
                (run_id,configuration_key,source_content_sha256,packet_id,packet_batch,packet_index,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, key, source["source_content_sha256"], packet["packet_id"], packet_batch, packet_index, now, now),
            )
        return key

    def record_audit(
        self,
        run_id: str,
        source_content_sha256: str,
        *,
        verdict: str,
        critical_failures: int,
        decision_sha256: str,
    ) -> str:
        if verdict not in {"PASS", "FAIL", "QUARANTINE"}:
            raise ValueError("invalid agent-audit verdict")
        with self.db:
            cursor = self.db.execute(
                """UPDATE factory_configuration_sample
                SET verdict=?, critical_failures=?, decision_sha256=?, updated_at=?
                WHERE run_id=? AND source_content_sha256=? AND verdict IS NULL""",
                (verdict, critical_failures, decision_sha256, time.time(), run_id, source_content_sha256),
            )
            if cursor.rowcount != 1:
                # Distinguish the two cases this guard used to conflate. An
                # unknown source is a real error. An already-audited source is a
                # re-audit: policy changed and we are deliberately re-deciding,
                # so overwrite rather than dead-letter the work item.
                known = self.db.execute(
                    """SELECT 1 FROM factory_configuration_sample
                    WHERE run_id=? AND source_content_sha256=?""",
                    (run_id, source_content_sha256),
                ).fetchone()
                if known is None:
                    raise ValueError("audit source is unknown")
                self.db.execute(
                    """UPDATE factory_configuration_sample
                    SET verdict=?, critical_failures=?, decision_sha256=?, updated_at=?
                    WHERE run_id=? AND source_content_sha256=?""",
                    (verdict, critical_failures, decision_sha256, time.time(),
                     run_id, source_content_sha256),
                )
            row = self.db.execute(
                "SELECT configuration_key FROM factory_configuration_sample WHERE run_id=? AND source_content_sha256=?",
                (run_id, source_content_sha256),
            ).fetchone()
        return row["configuration_key"]

    def decide(
        self,
        run_id: str,
        configuration_key: str,
        *,
        minimum_audits: int = 3,
        minimum_pass_rate: float = 0.80,
        minimum_wilson_lower: float = 0.35,
    ) -> ConfigurationDecision:
        if minimum_audits < 1 or not 0 <= minimum_pass_rate <= 1 or not 0 <= minimum_wilson_lower <= 1:
            raise ValueError("invalid qualification thresholds")
        existing = self.db.execute(
            "SELECT status,decision_json FROM factory_configuration WHERE run_id=? AND configuration_key=?",
            (run_id, configuration_key),
        ).fetchone()
        if existing is None:
            raise KeyError(configuration_key)
        if existing["status"] in {"QUALIFIED", "REJECTED"} and existing["decision_json"]:
            return ConfigurationDecision(**json.loads(existing["decision_json"]))
        rows = self.db.execute(
            "SELECT verdict,COALESCE(critical_failures,0) AS critical_failures FROM factory_configuration_sample WHERE run_id=? AND configuration_key=?",
            (run_id, configuration_key),
        ).fetchall()
        registered = len(rows)
        audited = sum(row["verdict"] is not None for row in rows)
        passed = sum(row["verdict"] == "PASS" for row in rows)
        failed = sum(row["verdict"] == "FAIL" for row in rows)
        quarantined = sum(row["verdict"] == "QUARANTINE" for row in rows)
        critical = sum(row["critical_failures"] for row in rows if row["verdict"] is not None)
        rate = passed / audited if audited else 0.0
        lower = wilson_lower(passed, audited)
        if audited < registered:
            status, reason = "PENDING", "registered audit sample is not complete"
        elif audited < minimum_audits:
            status, reason = "NEED_MORE", f"requires at least {minimum_audits} audited conversations"
        elif critical:
            status, reason = "REJECTED", "critical prompt/workflow failures observed"
        elif rate < minimum_pass_rate or lower < minimum_wilson_lower:
            status, reason = "REJECTED", "configuration pass evidence is below qualification thresholds"
        else:
            status, reason = "QUALIFIED", "completed audit sample satisfies configuration thresholds"
        decision = ConfigurationDecision(
            configuration_key, status, registered, audited, passed, failed, quarantined,
            critical, rate, lower, reason,
        )
        if status in {"QUALIFIED", "REJECTED"}:
            with self.db:
                self.db.execute(
                    "UPDATE factory_configuration SET status=?,decision_json=?,updated_at=? WHERE run_id=? AND configuration_key=?",
                    (status, json.dumps(decision.__dict__ if hasattr(decision, "__dict__") else {
                        field: getattr(decision, field) for field in decision.__dataclass_fields__
                    }, sort_keys=True), time.time(), run_id, configuration_key),
                )
        return decision

    def promotable(self, run_id: str, configuration_key: str) -> list[dict[str, Any]]:
        row = self.db.execute(
            "SELECT status FROM factory_configuration WHERE run_id=? AND configuration_key=?",
            (run_id, configuration_key),
        ).fetchone()
        if row is None or row["status"] != "QUALIFIED":
            return []
        return [dict(item) for item in self.db.execute(
            """SELECT * FROM factory_configuration_sample
            WHERE run_id=? AND configuration_key=? AND verdict='PASS' AND promoted=0
            ORDER BY created_at""",
            (run_id, configuration_key),
        ).fetchall()]

    def summary(self, run_id: str) -> dict[str, Any]:
        configuration_rows = self.db.execute(
            "SELECT status,COUNT(*) AS count FROM factory_configuration WHERE run_id=? GROUP BY status",
            (run_id,),
        ).fetchall()
        sample_rows = self.db.execute(
            "SELECT COALESCE(verdict, 'PENDING') AS verdict,COUNT(*) AS count FROM factory_configuration_sample WHERE run_id=? GROUP BY COALESCE(verdict, 'PENDING')",
            (run_id,),
        ).fetchall()
        return {
            "configurations": {row["status"]: row["count"] for row in configuration_rows},
            "conversation_audits": {row["verdict"]: row["count"] for row in sample_rows},
        }

    def mark_promoted(self, run_id: str, source_content_sha256: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE factory_configuration_sample SET promoted=1,updated_at=? WHERE run_id=? AND source_content_sha256=? AND promoted=0",
                (time.time(), run_id, source_content_sha256),
            )
            if cursor.rowcount != 1:
                raise ValueError("qualification sample was already promoted or is unknown")

    def samples(self, run_id: str) -> list[dict[str, Any]]:
        """Return source-bound audit rows with their strict configuration status."""
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT sample.*, configuration.status AS configuration_status
                FROM factory_configuration_sample AS sample
                JOIN factory_configuration AS configuration
                  ON configuration.run_id=sample.run_id
                 AND configuration.configuration_key=sample.configuration_key
                WHERE sample.run_id=?
                ORDER BY sample.created_at, sample.source_content_sha256""",
                (run_id,),
            ).fetchall()
        ]
