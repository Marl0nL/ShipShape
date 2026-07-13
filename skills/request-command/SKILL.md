---
name: request-command
description: Propose a command for the operator to run on the HOST (e.g. a gcloud IAM role grant the sandbox service account is missing). Use when you need a privileged/host-side action you cannot perform yourself — the operator reviews, edits, or declines it.
allowed-tools: [Bash]
---

# request-command

Some actions must happen on the host with the operator's credentials — most
commonly granting the sandbox service account an IAM role it is missing. You
cannot run these yourself. Propose the exact command:

```bash
request-command "<command>" [reason]
# e.g.
request-command \
  "gcloud projects add-iam-policy-binding market-operations \
     --member serviceAccount:agent-daily@market-operations.iam.gserviceaccount.com \
     --role roles/bigquery.dataViewer" \
  "need to read the analytics dataset for this task"
```

The command **blocks until the operator accepts, edits, or declines** it, then returns:
- `APPROVED` (exit 0) — the JSON includes the command's exit code and output.
- `DECLINED` (exit 3) — refused; adjust and propose again if it's still needed.
- `STILL PENDING` (exit 2) — re-run `await-request <id>` to keep waiting.

Run it with a generous Bash timeout. Propose the narrowest command that does the
job, and always give a clear reason.
