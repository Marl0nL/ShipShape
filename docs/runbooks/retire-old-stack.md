# Runbook: retire the old container stack

The previous design (one locked-down Docker container per stack, the
`shipshape` control plane, a Squid egress proxy) is no longer in the repo:
the tag `container-stack-final` keeps it (`git checkout container-stack-final`).
Retire a running stack once its work has moved to Ships.

1. **Check the tag.** The last commit of the old design is tagged
   `container-stack-final`; nothing else needs keeping in the repo. The tag is
   from before the stack moved into `legacy/`. The folder's final state (with
   its `LEGACY.md` note and two scrubbed example lines) is at commit `cdeecd7`,
   the last change to `legacy/` before it was deleted:
   `git checkout cdeecd7 -- legacy` brings it back.
2. **Copy what is worth keeping onto a Ship,** for example an instance's data
   folder:

   ```sh
   rsync -a --info=progress2 <instance-data-folder>/ agent@ship-01.local:work/<name>/
   ```

   Sign-ins are not migrated: sign the Ship in fresh
   ([sign-in.md](sign-in.md)).
3. **Revoke what the old stack used:**
   - the long-lived Claude setup-token (claude.ai, Settings, Claude Code tokens);
   - the fine-grained GitHub token (GitHub, Settings, Developer settings,
     Personal access tokens);
   - the agent service account's keys in Google Cloud (IAM, Service accounts,
     Keys), and the service account itself if nothing else uses it.
4. **Stop and remove it:**

   ```sh
   shipshape --instance <name> down
   docker image ls 'shipshape-agent-*'      # then docker image rm each
   pipx uninstall shipshape-controlplane
   ```

5. **Delete local leftovers** the old stack kept in its checkout (`auth/`,
   `instances/`, `control-plane/state/` and the per-instance data folders): they held
   credentials. They are git-ignored, so `git status` does not show them.
6. **Update or remove the old checkout.** The repo's `main` no longer has the
   old stack; anything you still run from it stays on `container-stack-final`
   until it is retired.
