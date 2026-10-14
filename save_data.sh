#!/usr/bin/env bash
set -euo pipefail
# Never commit sessions, runtime files or binary checkpoint videos.
git rm --cached -r --ignore-unmatch data/jobs >/dev/null
git config user.name video-bot
git config user.email video-bot@users.noreply.github.com
for path in data/videos data/settings.json data/telegram-ui.json characters; do
  if [[ -e "$path" ]]; then git add -- "$path"; fi
done
if git diff --cached --quiet; then exit 0; fi
if git diff --cached --diff-filter=ACMR --name-only | grep -Eq '(^private-runtime/|^state-backup/|^data/jobs/|(^|/)(cookies|genspark_session))'; then
  echo 'Unsafe credential/runtime path staged: refusing commit.'; exit 1
fi
git commit -m 'data: update history and non-secret settings'
for i in 1 2 3 4; do
  if git pull --rebase -q && git push -q; then exit 0; fi
  if [[ -d .git/rebase-merge || -d .git/rebase-apply ]]; then
    echo 'Concurrent data conflict: not overwriting data silently. Resolve conflict in repository.'
    exit 1
  fi
  sleep 3
done
echo 'Could not persist metadata to GitHub. Encrypted progress artifact must be retained.'
exit 1
