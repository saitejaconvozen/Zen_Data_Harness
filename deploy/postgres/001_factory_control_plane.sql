-- Zen conversation factory control-plane schema (PostgreSQL 16+).
-- Restricted conversation content belongs in the artifact store, never these rows.

CREATE TABLE IF NOT EXISTS factory_run (
  id uuid PRIMARY KEY,
  manifest jsonb NOT NULL,
  taxonomy_sha256 text NOT NULL,
  target_accepted integer NOT NULL CHECK (target_accepted > 0),
  status text NOT NULL CHECK (status IN
    ('PLANNED','RUNNING','PAUSED','NEEDS_HUMAN','SUCCEEDED','FAILED','CANCELLED')),
  accepted_count integer NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS factory_work (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES factory_run(id),
  job_key text NOT NULL,
  source_binding_sha256 text NOT NULL,
  stage text NOT NULL,
  revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
  priority integer NOT NULL DEFAULT 0,
  status text NOT NULL CHECK (status IN ('READY','LEASED','SUCCEEDED','DEAD','QUARANTINED')),
  payload jsonb NOT NULL,
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts integer NOT NULL CHECK (max_attempts > 0),
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  output_sha256 text,
  error_class text,
  error_detail text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (run_id, job_key, stage, revision),
  CHECK (
    (status = 'LEASED' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR status <> 'LEASED'
  )
);

CREATE INDEX IF NOT EXISTS factory_work_claim_idx
  ON factory_work (run_id, stage, priority DESC, available_at, created_at)
  WHERE status = 'READY';

CREATE INDEX IF NOT EXISTS factory_work_expired_lease_idx
  ON factory_work (lease_expires_at)
  WHERE status = 'LEASED';

CREATE TABLE IF NOT EXISTS factory_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES factory_run(id),
  work_id uuid REFERENCES factory_work(id),
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS conversation_lineage (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES factory_run(id),
  source_binding_sha256 text NOT NULL,
  split text CHECK (split IN ('TRAIN','BENCHMARK','QUARANTINE')),
  raw_artifact_sha256 text NOT NULL,
  current_candidate_sha256 text,
  refiner_session_id text,
  verifier_session_id text,
  verifier_verdict text CHECK (verifier_verdict IN ('PASS','FAIL','ABSTAIN')),
  repair_round integer NOT NULL DEFAULT 0 CHECK (repair_round >= 0),
  taxonomy_sha256 text NOT NULL,
  accepted_at timestamptz,
  UNIQUE (run_id, source_binding_sha256),
  CHECK (refiner_session_id IS NULL OR verifier_session_id IS NULL OR refiner_session_id <> verifier_session_id)
);

CREATE TABLE IF NOT EXISTS coverage_cell (
  run_id uuid NOT NULL REFERENCES factory_run(id),
  axis_key text NOT NULL,
  cell_key text NOT NULL,
  required_count integer NOT NULL CHECK (required_count >= 0),
  accepted_count integer NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
  PRIMARY KEY (run_id, axis_key, cell_key)
);

-- Atomic claim template used by the production adapter:
-- WITH candidate AS (
--   SELECT id FROM factory_work
--   WHERE run_id = $1 AND stage = ANY($2) AND status = 'READY'
--     AND available_at <= clock_timestamp() AND attempt < max_attempts
--   ORDER BY priority DESC, created_at
--   FOR UPDATE SKIP LOCKED LIMIT 1
-- )
-- UPDATE factory_work w
-- SET status='LEASED', attempt=attempt+1, lease_owner=$3, lease_token=$4,
--     lease_expires_at=clock_timestamp()+$5::interval, updated_at=clock_timestamp()
-- FROM candidate WHERE w.id=candidate.id RETURNING w.*;
