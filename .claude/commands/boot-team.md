You are the lead session, acting as Studio Director per CLAUDE.md.

Boot the wavealign team by:

1. Creating the team via `TeamCreate` with `team_name: "wavealign"`, `agent_type: "studio-director"`, and a one-line description noting today's date.
2. Spawning the two persistent teammates in a single response (one Agent call per region, in parallel):
   - `calibration-archivist` named `archive` — owns `memory/`. Tell it to do an initial scan and report back: is `memory/index.md` present? Are `memory/sessions/`, `memory/decisions/`, `memory/references/` present? List anything already encoded.
   - `safety-monitor` named `safety` — risk gate. Tell it to acknowledge online and stand by for fast scans on any proposed action that touches disk, an external system, or audio output.
3. Confirm to the user that the standing team is up, and prompt them to feed the first input — measurements, a code change idea, or a debug question.

Inline specialists (acoustician, measurement-engineer, dsp-engineer, filter-architect, mastering-ear, lab-technician) are invoked per-session as the pipeline calls for them. Do not spawn them as teammates here.

Do not run the full pipeline yet. Booting just spins up the persistent regions.
