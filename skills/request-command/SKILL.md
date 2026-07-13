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

It returns JSON with an `id` and `"status":"pending"`. The operator will
**accept, edit, or decline** it. Re-check the outcome:

```bash
curl -fsS "$SHIPSHAPE_BROKER/status/<id>"
```

On acceptance the response includes the command's exit status and output. Propose
the narrowest command that does the job, and always give a clear reason.
