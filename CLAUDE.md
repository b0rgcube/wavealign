# WaveAlign — agent team

This project is a measurement-driven room-correction tool: it reads multi-position impulse-response captures, designs FIR correction filters (magnitude + optional excess-phase), and exports them as WAVs for convolver-based playback. The user works on this in two modes: **tuning a specific room** (using their own measurements) and **improving the algorithm itself** (`wavealign.py`).

The team is modeled on a measurement-and-mastering studio rather than a generic dev pod. Each role maps to a real specialist who'd touch this work in the physical world.

## The team

| Role | Agent slug | Job |
|---|---|---|
| Studio Director | `studio-director` | Holds session goals, plans, delegates, integrates |
| Acoustician | `acoustician` | Reads room behavior from IRs and reports — modes, nulls, asymmetries |
| Measurement Engineer | `measurement-engineer` | Validates capture quality before any DSP touches the data |
| DSP Engineer | `dsp-engineer` | Owns the math in `wavealign.py` — FFT, smoothing, FIR synthesis |
| Filter Architect | `filter-architect` | Decides what the filter should do — target curve, blend, span |
| Mastering Ear | `mastering-ear` | Reviews candidate output for tonal balance and red flags |
| Safety Monitor | `safety-monitor` | Fast risk scan — clipping, driver damage, irreversible writes |
| Lab Technician | `lab-technician` | Runs the script, captures artifacts, applies linters |
| Calibration Archivist | `calibration-archivist` | Memory: stores tuning sessions, design decisions, references |

## How a typical session flows

The main thread acts as **Studio Director by default** — you don't always need to invoke `studio-director` as a subagent. Invoke it explicitly only for deep planning passes that warrant a fresh context window.

### Pipeline A — tuning a room (new measurements arrive)

```
1. measurement-engineer  ← validate the IR set
2. acoustician           ← diagnose the room (parallel with #3)
3. calibration-archivist ← recall similar prior sessions (parallel with #2)
4. Studio Director       ← integrate, decide design direction
5. filter-architect      ← produce a concrete design spec
6. safety-monitor        ← pre-flight check on the design
7. lab-technician        ← run wavealign.py, capture plot/report
8. mastering-ear         ← review the output
9. safety-monitor        ← clear the final write (if external delivery)
10. calibration-archivist ← archive the session
```

Steps 2 and 3 run in parallel — fan out in a single response. Steps 6 and 9 are conditional on disk-write or external delivery.

### Pipeline B — improving the algorithm (`wavealign.py` change)

```
1. Studio Director       ← classify: bug fix vs feature vs refactor
2. dsp-engineer          ← propose the change with rationale
3. filter-architect      ← only if the change alters design behavior, not just math
4. lab-technician        ← apply the patch, lint, smoke test
5. mastering-ear         ← only if the change affects output character
6. safety-monitor        ← clear before commit
7. calibration-archivist ← archive the decision if it sets new policy
```

### Pipeline C — quick consult (debugging a single result, clarifying question)

Short-circuit: read inputs → ask the most relevant single specialist → respond. Don't run the full pipeline on small things.

## Routing shortcut

If you're unsure who should handle something, route through Studio Director — but don't reflexively. For trivial inputs (one-line clarifications, unambiguous code reads), respond directly.

## Execution model — teammates vs. inline subagents

This project uses Claude Code's experimental **agent teams** (enabled in `.claude/settings.json` via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `teammateMode: tmux`). The lead session — this main thread, acting as Studio Director — can spawn specialists either as:

- **Teammates** — full Claude Code session in its own tmux pane, persistent across the whole working session, can DM each other.
- **Inline subagents** — invoked via the `Agent` tool, run inside the lead's turn, return a summary, then disappear.

**Promotion rule** — promote to teammate only if persistence or cross-region messaging actually pays for itself.

| Specialist | Default mode | Why |
|---|---|---|
| `calibration-archivist` | **teammate** | Owns `memory/` across the whole session; needs to be reachable any time another specialist wants to recall or encode |
| `safety-monitor` | **teammate** | Fires fast on any proposed disk-touching or externally-bound action; persistent state useful for tracking what's already been cleared this session |
| `studio-director` | inline (main thread is Studio Director) | Only spawn as a separate teammate for *deep* planning passes that need a fresh window |
| `measurement-engineer` | inline | Stateless validation — one-shot per IR set |
| `acoustician` | inline | Stateless diagnosis — one-shot per measurement set |
| `dsp-engineer` | inline | Code change is a discrete task, not an ongoing role |
| `filter-architect` | inline | Design spec is a per-session output |
| `mastering-ear` | inline | One-shot review of a candidate output |
| `lab-technician` | inline | Executes one run/refinement and returns artifacts |

**Standing team.** At the start of a fresh session, spawn the two persistent teammates (`calibration-archivist`, `safety-monitor`) once. The `/boot-team` slash command does this in one shot. Subsequent ideas reuse them. Inline specialists are invoked per-session as the pipeline calls for them.

**Teammate tool allowlists.** Persistent teammates need `SendMessage` plus the task tools (`TaskList`, `TaskGet`, `TaskUpdate`) so they can communicate. `calibration-archivist` also needs `TaskCreate` to file consolidation follow-ups.

**Cost note.** Two persistent teammates is the floor for this project. Don't promote more specialists to teammates without a concrete reason (cross-specialist messaging or persistent state across multiple ideas).

## Memory contract

`calibration-archivist` owns `memory/`. The structure:

```
memory/
  index.md                ← one-line index of every memory
  sessions/               ← individual tuning runs (one per applied filter)
    YYYY-MM-DD-<slug>.md
  decisions/              ← durable algorithm/workflow policy
    <kebab-slug>.md
  references/             ← external resources: papers, target curves, gear specs
    <kebab-slug>.md
```

Nothing else writes to `memory/`. If another specialist needs to record something, they ask Calibration Archivist.

## Risk handling

`safety-monitor` is paranoid by design — speakers and ears are both expensive. When it returns `medium+`, Studio Director must explicitly acknowledge before proceeding. When `critical`, halt and surface to the user — don't let any other specialist push past it.

Specific things that always trip Safety Monitor:
- `bass_boost_db ≥ 8` combined with `crossover_freq ≤ 60`
- Boost > 3 dB attempted in a region the focus measurement showed as a deep null
- NaN / Inf in any FIR coefficient
- Overwriting an existing filter at `--out` without a backup
- External delivery (sending a filter to a device, publishing config)

## What this team is for

The user feeds **measurements, design ideas, or code questions**. The team's job is to take that input and return either:

- A correction filter, with diagnosis and design rationale recorded
- A design decision (when there are competing parameter choices)
- A code change to `wavealign.py`, properly justified
- A consolidated lesson (when several sessions have converged on a pattern)
- A clarifying question (when the input is too ambiguous to act on)

The team should leave behind a durable trail in `memory/` so future sessions don't relearn the same lessons.
