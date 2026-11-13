import GitBackend

/-!
# Export the state machine for the Python implementation's tests

Explores every state reachable from `Task.init` within a few events, over a small alphabet
(workers 1–3, epochs 0–5, PRs 7 and 8), and prints, for each state, every event `step`
accepts and the state it produces. Everything else in the alphabet is denied.

    lake exe export_step > ../../tests/fixtures/lean_step.json
-/

open Lean Coord

def alphabet : List Event :=
  let ws := [1, 2, 3]
  let es := [0, 1, 2, 3, 4, 5]
  (ws.flatMap fun w => es.flatMap fun e =>
    [.claim w e, .heartbeat w e, .report w e (.success 7), .report w e (.part 8), .report w e .failure,
     .review w e .approve, .review w e .changes, .review w e .abstain])
  ++ es.map .expire ++ [.accept, .rework, .cancel]

partial def explore (frontier : List Task) (seen : Array Task) (depth : Nat) : Array Task :=
  if depth == 0 || frontier.isEmpty then seen else
  let (seen', fresh) := frontier.foldl (init := (seen, ([] : List Task))) fun acc t =>
    alphabet.foldl (init := acc) fun (s, f) ev =>
      match step t ev with
      | some t' => if s.contains t' then (s, f) else (s.push t', t' :: f)
      | none => (s, f)
  explore fresh seen' (depth - 1)

def main : IO Unit := do
  let states := explore [Task.init] #[Task.init] 8
  let evs := alphabet.toArray
  let cases := states.map fun t =>
    let acc := (List.range evs.size).filterMap fun i =>
      (step t evs[i]!).map fun t' => Json.arr #[toJson i, toJson t']
    Json.mkObj [("state", toJson t), ("accepted", Json.arr acc.toArray)]
  IO.println (Json.mkObj [("alphabet", toJson evs), ("cases", Json.arr cases)]).compress
