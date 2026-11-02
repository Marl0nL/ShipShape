# Runbook: retire the old container stack

The previous design (one locked-down Docker container per stack, the
`shipshape` control plane, a Squid egress proxy) lives in `legacy/` and is
removed after phase 2. Retire a running stack once its work has moved to Ships.

1. **Check the tag.** The last commit of the old design is tagged
   `container-stack-final`; nothing else needs keeping in the repo.
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
6. **After phase 2's checks pass,** `legacy/` is deleted from the repo in one
   commit.
