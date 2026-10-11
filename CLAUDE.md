# Atlas

Atlas is a local control room and a tamper-evident record over work done in Claude Code sessions.
Read `README.md` first, then `docs/v2/contracts.md` (the binding contracts).

## Rules for any session working in this repository

- The ledger and the installed hooks folder are protected: read them with the Read or Grep tools, change them
  only through the documented tools in `tools/`.
- Tests never touch live data. Use temp directories and the fixtures; point the server at synthetic files with the
  `ATLAS_*_FILE` environment variables.
- Python 3.12, standard library plus `pyyaml`. No build step for the front end.
- Run one suite at a time while iterating (`py -3.12 -m pytest -q cockpit/ledger2`), the whole suite before a commit.
- UI copy is impersonal (no "you" or "your"), has no em-dashes, and disabled controls say why they are disabled.
- Colours change only through `areas.yaml` / `cockpit/web2/look.css` and must pass `py -3.12 tools/check_look.py`.
