# Multi-agent and multi-person workflows

The failure mode with several agents (or a designer and a 3D artist) on one model
is silent divergence: agent B regenerates from a prompt agent A already
superseded, or a reviewer approves a mesh that never passed the print profile.

forge3d's answer is a **handoff packet** — one small JSON object that names the
spec, the artifacts, the checks, the owners, the open questions and who is next.
It is small enough to paste into any context window and complete enough that
nobody has to re-derive anything.

## Roles that work

| Role | Does | Ends with |
|---|---|---|
| **brief agent** | turns the request into a `ModelSpec`, asks the human for missing fields | `plan_model` output |
| **geometry agent** | writes the source for the chosen backend, iterates until it builds | a mesh path + `create_model` result |
| **validation agent** | runs `validate_model` / `review_model`, fixes with `repair_model` | a passing report |
| **viewer agent** | produces previews and the shareable page, checks it on a phone viewport | an HTML path |
| **review agent / human** | decides ship or revise against the checklist | a handoff with `status: approved` |

Splitting geometry from validation is the highest-value split: the agent that
built the mesh is the worst judge of whether it is correct.

## The packet

```json
{
  "id": "497451f3",
  "title": "side chair",
  "status": "in_review",
  "spec": { "name": "chair", "purpose": "game", "units": "mm",
            "dimensions": {"x": 450, "y": 480, "z": 850}, "poly_budget": 60000 },
  "backend": "procedural",
  "artifacts": { "model": "chair.glb", "viewer": "chair.html",
                 "previews": "previews/chair_iso.png" },
  "checks": { "watertight": true, "uv_mapping": false, "lod_chain": false },
  "owners": { "design": "priya", "geometry": "agent-a" },
  "open_questions": ["does the client need a print-ready STL too?"],
  "next_agent": "validation-agent",
  "notes": ["8 bodies — merge before LOD generation"]
}
```

Produced by:

```python
create_handoff(
    spec=spec,
    artifacts={"model": "chair.glb", "viewer": "chair.html"},
    backend="procedural",
    checks=checklist("game"),
    owners={"design": "priya", "geometry": "agent-a"},
    next_agent="validation-agent",
)
```

It writes two files: `{id}.json` for agents and `{id}.md` for humans. The Markdown
is a ticket-ready summary — paste it into GitHub, Slack or a review doc.

## Rules that keep a fleet of agents sane

1. **One owner per artifact.** Two agents writing `chair.glb` is a merge conflict
   with no diff. Give each agent its own output name (`chair-a.glb`) and let a
   review step pick the winner.
2. **Pass the packet, not the history.** A new agent needs the spec, the paths
   and the failing checks — not the conversation that produced them.
3. **Never re-derive the spec.** If the spec is wrong, edit the spec and bump the
   handoff `status` back to `draft`. Silently changing dimensions downstream is
   how a print fails at 3 a.m.
4. **Checks are booleans, not prose.** "Looks fine" does not merge. `watertight:
   true` does.
5. **Record the backend.** A mesh from `hosted` and a mesh from `openscad` have
   completely different reliability profiles; the next agent needs to know which
   it inherited.
6. **Open questions are first-class.** An empty list means "I resolved
   everything", which is a claim someone can verify.

## Statuses

`draft` → `in_review` → `blocked` | `approved` → `shipped`

`blocked` must come with an open question or a failing check. `approved` requires
a passing validation report for the target profile. `shipped` requires a delivery
checklist with no unticked items.

## Context-window discipline across agents

Each agent should carry at most:

- the handoff packet (≈400 tokens)
- one model card per artifact (≈300 tokens each)
- the failing checks with their hints

That is under 2k tokens for a complete picture of a model. Compare with pasting
a GLB — which is not possible — or describing a mesh from memory, which is how
agents confidently report the wrong triangle count.
