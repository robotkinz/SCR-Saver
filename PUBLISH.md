# Publishing SCR Saver

Two folders:

- `~/Projects/scrsaver` — live development. Edit here.
- `~/SCR Saver Project` — GitHub snapshot. Open this in GitHub Desktop.

[https://github.com/robotkinz/SCR-Saver](https://github.com/robotkinz/SCR-Saver)

## Each time you are ready to post an update

1. In the live tree, bump `__version__` in `src/scrsaver/__init__.py`.
2. Add a new section at the top of `CHANGELOG.md`. Keep the bullets short enough to paste into a social post.
3. Run `./sync-github-folder.sh` from the live tree.
4. Copy `SOCIAL.md` into your social posts.
5. In GitHub Desktop, open **SCR Saver Project**, commit, and push.

`SOCIAL.md` is rewritten from the latest `CHANGELOG.md` section. You only maintain the changelog.

The sync copies source into the GitHub folder and leaves `.git` and `dist/` alone. It does not commit or push.

## What stays private

Do not copy these into the GitHub folder or the public tarball:

- `~/.local/share/scrsaver/library`
- `~/.config/scrsaver/config.json`
- Wine prefixes under `~/.local/share/scrsaver/`

`./package.sh` builds a clean archive. `./package.sh --with-user-data` is only for a USB copy of this machine’s imported savers.
