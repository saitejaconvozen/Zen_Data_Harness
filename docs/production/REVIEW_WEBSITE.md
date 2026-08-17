# Protected conversation review website

The `review-website` plugin converts a validated
`zen.golden-review-batch/1` artifact into a browser UI. It displays exact user
turns, source and golden assistant turns, edit reasons, independent-verifier
status, and named axis/subaxis/variant citations.

## Build through the harness

```bash
.venv/bin/zen --root . run \
  "Build review website for verified generated conversations" \
  --workflow golden-review-website \
  --input review_batch=.zen/jobs/RUN_ID/human-review-batch.json
```

The run result gives an owner-only site directory under `.zen/sites/`.

## Local hosting

```bash
.venv/bin/python plugins/review-website/scripts/serve.py \
  --site .zen/sites/SITE_RUN_ID
```

The server binds only to `127.0.0.1`, generates an access token, and prints a
private URL. Use SSH port forwarding if the harness runs on a remote host.

## ngrok publication

External publication is an explicit human action because the site contains
restricted conversation data:

```bash
.venv/bin/python plugins/review-website/scripts/publish_ngrok.py \
  --site .zen/sites/SITE_RUN_ID
```

If `NGROK_AUTHTOKEN` is not present, the harness asks for it using hidden
terminal input. The token is supplied to ngrok only through the child-process
environment and is not written to the repository, SQLite, artifacts, logs, or
the command line. `ZEN_REVIEW_TOKEN` may be supplied separately; otherwise the
harness generates one. The resulting ngrok URL remains protected by the review
server token.

Do not paste an ngrok authtoken into chat, source code, `.env` committed to Git,
or a command-line argument. Revoke it in the ngrok dashboard if exposure is
suspected.
