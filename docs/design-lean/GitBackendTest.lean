import GitBackend

/-!
# Does git (and GitHub) support the operations the design needs?

Run with `lake test`. The suite always runs against a throwaway local bare repository
configured like the production ruleset (no non-fast-forward, no deletion).

The remote run is optional. Give a remote to also run the same suite against it:

    lake test -- --remote git@github.com:OWNER/REPO.git
    AFE_FLEET_TEST_REMOTE=git@github.com:OWNER/REPO.git lake test

It works under `task-lock-test/<run>/` and deletes its branches afterwards. Without a
remote it is skipped and says so. `--remote-only` skips the local run.

What is exercised, always with real pushes:

1. **Every transition.** Seven states × every event (149 pairs). The expected result of each
   pair is written out by hand below; the Lean model must agree with the table, and every
   accepted pair must land on the remote in both push modes (`--force-with-lease`, and plain
   fast-forward-only).
2. **Every denied update, at the rule level.** For each denied pair the client writes
   nothing. Then a misbehaving client pushes it anyway: git accepts it (git does not know
   the rules) and the audit flags exactly that commit.
3. **Every denied update, at the git level.** After each accepted transition, a second
   client that read the same state tries every transition that was legal in that state.
   Git must reject all of them, in both push modes.
4. **Ref-level denials.** Creating a lock branch that already exists, wrong expected tip,
   unrelated history; and (local remote only) forced rewrite and deletion.
5. **A real race.** Several clients claim the same unclaimed task concurrently, each
   creating its lock branch: exactly one wins.

An unclaimed task has no lock branch; its first claim creates it.
6. **End to end.** The 20-event "nothing fails" trace and a zombie scenario, one push per
   event, a separate object store per worker, then an audit of the whole branch.
-/

open Coord Git

structure Tally where
  pass : IO.Ref Nat
  fail : IO.Ref Nat

def Tally.check (t : Tally) (ok : Bool) (what : String) : IO Unit := do
  if ok then t.pass.modify (· + 1)
  else
    t.fail.modify (· + 1)
    IO.eprintln s!"  FAIL: {what}"

/-! ## The hand-written transition table -/

namespace L
def claimNew    := "claim by a new worker"
def claim1      := "claim by worker 1"
def claimFuture := "claim, future epoch"
def claimStale  := "claim, stale epoch"
def hb          := "heartbeat by holder"
def hbOther     := "heartbeat by non-holder"
def hbStale     := "heartbeat, stale token"
def repS        := "report success by holder"
def repP        := "report partial by holder"
def repF        := "report failure by holder"
def repOther    := "report by non-holder"
def repStale    := "report, stale token"
def revA        := "review approve by holder"
def revC        := "review changes by holder"
def revAb       := "review abstain by holder"
def revOther    := "review by non-holder"
def revStale    := "review, stale token"
def exp         := "expire current lease"
def expStale    := "expire, stale token"
def expFuture   := "expire, future token"
def accept      := "accept"
def rework      := "rework"
end L

/-- Every event shape, instantiated for a state with epoch `e`. `h` is the lease holder, or
for an unleased state the most recent holder (so those rows are the zombie's writes).
Worker 9 has never touched the task. `none` = not applicable (no older epoch exists). -/
def probes (t : Task) (h : Worker) : List (String × Option Event) :=
  let e := t.epoch
  let old (ev : Event) : Option Event := if e > 0 then some ev else none
  [ (L.claimNew,    some (.claim 9 e)),
    (L.claim1,      some (.claim 1 e)),
    (L.claimFuture, some (.claim 9 (e + 1))),
    (L.claimStale,  old (.claim 9 (e - 1))),
    (L.hb,          some (.heartbeat h e)),
    (L.hbOther,     some (.heartbeat 9 e)),
    (L.hbStale,     old (.heartbeat h (e - 1))),
    (L.repS,        some (.report h e (.success 8))),
    (L.repP,        some (.report h e (.part 8))),
    (L.repF,        some (.report h e .failure)),
    (L.repOther,    some (.report 9 e (.success 8))),
    (L.repStale,    old (.report h (e - 1) (.success 8))),
    (L.revA,        some (.review h e .approve)),
    (L.revC,        some (.review h e .changes)),
    (L.revAb,       some (.review h e .abstain)),
    (L.revOther,    some (.review 9 e .approve)),
    (L.revStale,    old (.review h (e - 1) .approve)),
    (L.exp,         some (.expire e)),
    (L.expStale,    old (.expire (e - 1))),
    (L.expFuture,   some (.expire (e + 1))),
    (L.accept,      some .accept),
    (L.rework,      some .rework) ]

structure St where
  name    : String
  /-- Events that build this state from an unclaimed task (no lock branch). -/
  path    : List Event
  /-- The state, written out. Fields: need, epoch, lease, pr, author, strikes. -/
  task    : Task
  /-- Holder, or last holder if unleased. -/
  actor   : Worker
  /-- The only events accepted here, with the status each must produce.
  Every other probe must be denied. -/
  accepts : List (String × Task)
  deriving Inhabited

def states : Array St := #[
  { name := "unclaimed", path := [], actor := 1,
    task := ⟨.work, 0, none, none, none, 0⟩,
    accepts := [
      (L.claimNew, ⟨.work, 1, some 9, none, none, 0⟩),
      (L.claim1,   ⟨.work, 1, some 1, none, none, 0⟩) ] },
  -- worker 3 claimed and was expired
  { name := "work", path := [.claim 3 0, .expire 1], actor := 3,
    task := ⟨.work, 1, none, none, none, 1⟩,
    accepts := [
      (L.claimNew, ⟨.work, 2, some 9, none, none, 1⟩),
      (L.claim1,   ⟨.work, 2, some 1, none, none, 1⟩) ] },
  { name := "work+lease", path := [.claim 3 0, .expire 1, .claim 1 1], actor := 1,
    task := ⟨.work, 2, some 1, none, none, 1⟩,
    accepts := [
      (L.hb,   ⟨.work,   2, some 1, none,   none,   1⟩),
      (L.repS, ⟨.review, 2, none,   some 8, some 1, 1⟩),
      (L.repP, ⟨.work,   2, none,   some 8, none,   1⟩),
      (L.repF, ⟨.work,   2, none,   none,   none,   2⟩),
      (L.exp,  ⟨.work,   2, none,   none,   none,   2⟩) ] },
  { name := "review",
    path := [.claim 3 0, .expire 1, .claim 1 1, .report 1 2 (.success 7)], actor := 1,
    task := ⟨.review, 2, none, some 7, some 1, 1⟩,
    accepts := [
      (L.claimNew, ⟨.review, 3, some 9, some 7, some 1, 1⟩),
      (L.accept,   ⟨.done,   2, none,   some 7, some 1, 1⟩),
      (L.rework,   ⟨.work,   2, none,   some 7, some 1, 1⟩) ] },
  { name := "review+lease",
    path := [.claim 3 0, .expire 1, .claim 1 1, .report 1 2 (.success 7), .claim 2 2], actor := 2,
    task := ⟨.review, 3, some 2, some 7, some 1, 1⟩,
    accepts := [
      (L.hb,     ⟨.review, 3, some 2, some 7, some 1, 1⟩),
      (L.revA,   ⟨.merge,  3, none,   some 7, some 1, 1⟩),
      (L.revC,   ⟨.work,   3, none,   some 7, some 1, 1⟩),
      (L.revAb,  ⟨.review, 3, none,   some 7, some 1, 1⟩),
      (L.exp,    ⟨.review, 3, none,   some 7, some 1, 2⟩),
      (L.accept, ⟨.done,   3, none,   some 7, some 1, 1⟩),
      (L.rework, ⟨.work,   3, none,   some 7, some 1, 1⟩) ] },
  { name := "merge",
    path := [.claim 3 0, .expire 1, .claim 1 1, .report 1 2 (.success 7), .claim 2 2,
             .review 2 3 .approve], actor := 2,
    task := ⟨.merge, 3, none, some 7, some 1, 1⟩,
    accepts := [
      (L.accept, ⟨.done, 3, none, some 7, some 1, 1⟩),
      (L.rework, ⟨.work, 3, none, some 7, some 1, 1⟩) ] },
  { name := "done",
    path := [.claim 3 0, .expire 1, .claim 1 1, .report 1 2 (.success 7), .claim 2 2,
             .review 2 3 .approve, .accept], actor := 2,
    task := ⟨.done, 3, none, some 7, some 1, 1⟩,
    accepts := [] } ]

/-- One (state, event) pair. -/
structure Case where
  st    : St
  seed  : Option String -- tip commit of the branch holding `st`; `none` = no branch
  idx   : Nat
  label : String
  ev    : Event
  want  : Option Task   -- `none` = must be denied
  deriving Inhabited

/-! ## Harness -/

structure Ctx where
  tally   : Tally
  root    : System.FilePath
  remote  : String
  pfx     : String
  isLocal : Bool
  a       : Client   -- honest writer
  b       : Client   -- competing writer
  r       : Client   -- independent reader / auditor

def Ctx.client (x : Ctx) (name : String) : IO Client := Client.create name x.root x.remote x.pfx

def tipOf (tips : Array (String × String)) (task : String) : Option String :=
  (tips.find? (·.1 == task)).map (·.2)

def needName : Need → String
  | .work => "work" | .review => "review" | .merge => "merge" | .done => "done"

def pad (s : String) (n : Nat) : String := s ++ String.ofList (List.replicate (n - s.length) ' ')

/-- Build one branch per state by pushing its path one event at a time. The first event
creates the branch; the unclaimed state has none. -/
def buildSeeds (x : Ctx) : IO (Array (St × Option String)) := do
  let mut cur : Array (Option String × Task) := states.map fun _ => (none, Task.init)
  let depth := states.foldl (fun m s => max m s.path.length) 0
  for d in [0:depth] do
    let mut stepUps : Array Upd := #[]
    let mut next := cur
    for i in [0:states.size] do
      let s := states[i]!
      let (sha, t) := cur[i]!
      if let some ev := s.path[d]? then
        match step t ev with
        | none => x.tally.check false s!"seed {s.name}: path event {repr ev} denied by the model"
        | some t' =>
          let sha' ← x.a.commit t' sha (← x.a.eventMsg ev)
          stepUps := stepUps.push ⟨x.a.ref s!"seed/{s.name}", sha, some sha'⟩
          next := next.set! i (some sha', t')
    for ok in ← x.a.push .lease stepUps do
      x.tally.check ok s!"seed step {d} accepted"
    cur := next
  -- an independent client reads them back through `snapshot()`
  let snap ← x.r.snapshot
  let mut res := #[]
  for i in [0:states.size] do
    let s := states[i]!
    let (sha, _) := cur[i]!
    match snap.find? (·.1 == s!"seed/{s.name}"), sha with
    | some (_, sn), some _ =>
      x.tally.check (sn.rev == sha) s!"seed {s.name}: reader sees the pushed tip"
      x.tally.check (sn.val == s.task) s!"seed {s.name}: status on the remote is the hand-written state"
      x.tally.check ((← x.r.audit sn.rev.get!).isEmpty) s!"seed {s.name}: history is a legal run"
    | none, none =>
      let sn ← x.r.read s!"seed/{s.name}"
      x.tally.check (sn.rev.isNone && sn.val == s.task) s!"seed {s.name}: no branch reads as unclaimed"
    | _, _ => x.tally.check false s!"seed {s.name}: snapshot disagrees with what was pushed"
    res := res.push (s, sha)
  return res

def allCases (seeds : Array (St × Option String)) : Array Case := Id.run do
  let mut cs := #[]
  for (s, sha) in seeds do
    let mut i := 0
    for (label, ev?) in probes s.task s.actor do
      if let some ev := ev? then
        cs := cs.push { st := s, seed := sha, idx := i, label, ev, want := s.accepts.lookup label }
      i := i + 1
  return cs

/-- Create one fresh branch per case, all pointing at that case's state. One push.
Cases in the unclaimed state get no branch. -/
def fork (x : Ctx) (names : Array (String × Option String)) : IO Unit := do
  let ups := names.filterMap fun (name, sha) => sha.map fun s => (⟨x.a.ref name, none, some s⟩ : Upd)
  for ok in ← x.a.push .lease ups do
    x.tally.check ok "fork (create-only push) accepted"

def printMatrix (seeds : Array (St × Option String)) : IO Unit := do
  IO.println ""
  IO.println <| pad "" 28 ++ String.join (states.toList.map fun s => pad s.name 14)
  let labels := (probes Task.init 1).map (·.1)
  for label in labels do
    let mut row := pad label 28
    for (s, _) in seeds do
      let cell := match (probes s.task s.actor).lookup label with
        | some (some _) =>
          match s.accepts.lookup label with
          | some t' => s!"→ {needName t'.need}" ++ (if t'.lease.isSome then "+lease" else "")
          | none => "denied"
        | _ => "n/a"
      row := row ++ pad cell 14
    IO.println row
  IO.println ""

/-- Sections 1 and 2: the model agrees with the table; accepted pairs land; denied pairs are
refused by an honest client and flagged by the audit when forced through. -/
def ruleMatrix (x : Ctx) (cases : Array Case) : IO (Nat × Nat) := do
  -- the Lean model must agree with the hand-written table, pair by pair
  for c in cases do
    x.tally.check (step c.st.task c.ev == c.want)
      s!"model vs table: {c.st.name} / {c.label}"
  for s in states do
    for (label, _) in s.accepts do
      x.tally.check (cases.any fun c => c.st.name == s.name && c.label == label)
        s!"table row '{label}' of {s.name} is a known probe"
  let acc := cases.filter (·.want.isSome)
  let den := cases.filter (·.want.isNone)
  -- accepted pairs: push in both modes, read back
  for mode in [Mode.lease, Mode.ffOnly] do
    let name (c : Case) := s!"acc/{mode.label}/{c.st.name}/{c.idx}"
    fork x (acc.map fun c => (name c, c.seed))
    let mut ups : Array Upd := #[]
    for c in acc do
      let sha ← x.a.commit c.want.get! c.seed (← x.a.eventMsg c.ev)
      ups := ups.push ⟨x.a.ref (name c), c.seed, some sha⟩
    let rs ← x.a.push mode ups
    let tips ← x.r.tips
    x.r.fetchAll
    for i in [0:acc.size] do
      let c := acc[i]!
      let what := s!"{mode.label}: {c.st.name} / {c.label}"
      x.tally.check rs[i]! s!"{what}: push accepted"
      match tipOf tips (name c) with
      | some tip =>
        x.tally.check (some tip == ups[i]!.commit) s!"{what}: remote tip is the pushed commit"
        x.tally.check (some (← x.r.statusAt tip) == c.want) s!"{what}: remote status is the expected one"
        x.tally.check ((← x.r.audit tip).isEmpty) s!"{what}: history is a legal run"
      | none => x.tally.check false s!"{what}: branch missing"
  -- denied pairs, honest client: `submit` pushes nothing and the branch does not move
  let before ← x.r.tips
  for c in den do
    let res ← x.a.submit .lease s!"seed/{c.st.name}" ⟨c.seed, c.st.task⟩ c.ev
    x.tally.check (res == .denied) s!"honest client refuses: {c.st.name} / {c.label}"
  let after ← x.r.tips
  for (s, sha) in (cases.map fun c => (c.st, c.seed)) do
    x.tally.check (tipOf before s!"seed/{s.name}" == sha && tipOf after s!"seed/{s.name}" == sha)
      s!"{s.name}: branch did not move"
  -- denied pairs, misbehaving client: writes the commit anyway (status unchanged, event
  -- recorded). Git has no way to know; the audit must flag exactly this commit.
  let name (c : Case) := s!"forged/{c.st.name}/{c.idx}"
  fork x (den.map fun c => (name c, c.seed))
  let mut ups : Array Upd := #[]
  for c in den do
    let sha ← x.b.commit c.st.task c.seed (← x.b.eventMsg c.ev)
    ups := ups.push ⟨x.b.ref (name c), c.seed, some sha⟩
  let rs ← x.b.push .lease ups
  x.r.fetchAll
  for i in [0:den.size] do
    let c := den[i]!
    let what := s!"forged: {c.st.name} / {c.label}"
    x.tally.check rs[i]! s!"{what}: git accepts the push (git does not know the rules)"
    let bad ← x.r.audit ups[i]!.commit.get!
    x.tally.check (bad.size == 1 && (bad[0]!.splitOn "not a legal step").length == 2)
      s!"{what}: audit flags exactly that commit (got {bad})"
  return (acc.size, den.size)

/-- Section 3: after each accepted transition, every write based on the old tip is rejected. -/
def staleMatrix (x : Ctx) (cases : Array Case) : IO Nat := do
  let acc := cases.filter (·.want.isSome)
  let mut total := 0
  for mode in [Mode.lease, Mode.ffOnly] do
    let mut pairs : Array (Case × Case) := #[]
    for c₁ in acc do
      for c₂ in acc do
        if c₁.st.name == c₂.st.name then pairs := pairs.push (c₁, c₂)
    let name (p : Case × Case) := s!"stale/{mode.label}/{p.1.st.name}/{p.1.idx}-{p.2.idx}"
    fork x (pairs.map fun p => (name p, p.1.seed))
    let mut first : Array Upd := #[]
    let mut second : Array Upd := #[]
    for p in pairs do
      let (c₁, c₂) := p
      let s₁ ← x.a.commit c₁.want.get! c₁.seed (← x.a.eventMsg c₁.ev)
      let s₂ ← x.b.commit c₂.want.get! c₂.seed (← x.b.eventMsg c₂.ev)
      first := first.push ⟨x.a.ref (name p), c₁.seed, some s₁⟩
      second := second.push ⟨x.b.ref (name p), c₂.seed, some s₂⟩
    let r₁ ← x.a.push mode first
    let r₂ ← x.b.push mode second
    let tips ← x.r.tips
    for i in [0:pairs.size] do
      let p := pairs[i]!
      let what := s!"{mode.label}: {p.1.st.name}: '{p.1.label}' lands, then stale '{p.2.label}'"
      x.tally.check r₁[i]! s!"{what}: first push accepted"
      x.tally.check (!r₂[i]!) s!"{what}: second push rejected by git"
      x.tally.check (tipOf tips (name p) == first[i]!.commit) s!"{what}: tip is still the first writer's"
    total := total + pairs.size
  return total

/-- Section 4: denials that are about the ref, not about a transition. -/
def refDenials (x : Ctx) (seeds : Array (St × Option String)) : IO Unit := do
  let some (_, some workSha) := seeds.find? (·.1.name == "work") | return
  let some (_, some doneSha) := seeds.find? (·.1.name == "done") | return
  -- a first claim, as if the task were unclaimed
  let root ← x.b.commit ⟨.work, 1, some 9, none, none, 0⟩ none (← x.b.eventMsg (.claim 9 0))
  let child ← x.b.commit ⟨.work, 2, some 9, none, none, 1⟩ (some workSha) (← x.b.eventMsg (.claim 9 1))
  let r ← x.b.push .lease #[⟨x.b.ref "seed/work", none, some root⟩]
  x.tally.check (!r[0]!) "creating a lock branch that already exists is rejected (create-only)"
  let r ← x.b.push .lease #[⟨x.b.ref "seed/work", some doneSha, some child⟩]
  x.tally.check (!r[0]!) "a correct child commit with the wrong expected tip is rejected"
  let r ← x.b.push .ffOnly #[⟨x.b.ref "seed/done", none, some root⟩]
  x.tally.check (!r[0]!) "plain push of unrelated history is rejected (not a fast-forward)"
  if x.isLocal then
    let r ← x.b.push .force #[⟨x.b.ref "seed/done", none, some root⟩]
    x.tally.check (!r[0]!) "forced rewrite is rejected by the remote (ruleset)"
    let r ← x.b.push .ffOnly #[⟨x.b.ref "seed/done", none, none⟩]
    x.tally.check (!r[0]!) "deleting a lock branch is rejected by the remote (ruleset)"
  else
    IO.println "  skipped: forced rewrite / deletion (needs a ruleset on the remote)"
  let tips ← x.r.tips
  x.tally.check (tipOf tips "seed/work" == some workSha && tipOf tips "seed/done" == some doneSha)
    "branches are untouched after the rejected pushes"

/-- Section 5: a real concurrent race. -/
def liveRace (x : Ctx) (n rounds : Nat) : IO Nat := do
  let mut clients := #[]
  for i in [0:n] do
    clients := clients.push (← x.client s!"racer{i + 1}")
  let mut races := 0
  for mode in [Mode.lease, Mode.ffOnly] do
    for round in [0:rounds] do
      let task := s!"race/{mode.label}/{round}"
      let mut snaps := #[]
      for c in clients do
        snaps := snaps.push (← c.read task)
      x.tally.check (snaps.all (·.rev.isNone)) "race: the task starts with no lock branch"
      let mut jobs := #[]
      for i in [0:n] do
        jobs := jobs.push (← IO.asTask (prio := .dedicated)
          (clients[i]!.submit mode task snaps[i]! (.claim (i + 1) 0)))
      let mut winners : Array Nat := #[]
      let mut lost := 0
      for i in [0:n] do
        match ← IO.ofExcept jobs[i]!.get with
        | .won _ => winners := winners.push (i + 1)
        | .lost => lost := lost + 1
        | .denied => pure ()
      let what := s!"race {mode.label} #{round}"
      x.tally.check (winners.size == 1) s!"{what}: exactly one of {n} concurrent claims wins (got {winners})"
      x.tally.check (lost == n - 1) s!"{what}: every other claim is rejected by git"
      let s ← x.r.read task
      x.tally.check (s.val.lease == winners[0]? && s.val.epoch == 1) s!"{what}: the lease belongs to the winner"
      match s.rev with
      | some rev => x.tally.check ((← x.r.audit rev).isEmpty) s!"{what}: history is a legal run"
      | none => x.tally.check false s!"{what}: no lock branch after the race"
      races := races + 1
  return races

def actorOf : Event → Nat
  | .claim w _ | .heartbeat w _ | .report w _ _ | .review w _ _ => w
  | _ => 0

/-- Section 6: whole lifecycles, one read and one push per event, one client per worker. -/
def endToEnd (x : Ctx) : IO Nat := do
  let who := #[← x.client "admin", ← x.client "w1", ← x.client "w2", ← x.client "w3"]
  let trace : List Event :=
    [.claim 1 0, .expire 1,
     .claim 2 1, .report 2 2 (.part 7),
     .claim 3 2, .report 3 3 .failure,
     .claim 1 3, .report 1 4 (.success 8),
     .claim 2 4, .review 2 5 .changes,
     .claim 3 5, .report 3 6 (.success 8),
     .claim 1 6, .review 1 7 .approve,
     .rework,
     .claim 2 7, .report 2 8 (.success 8),
     .claim 3 8, .review 3 9 .approve,
     .accept]
  let mut pushes := 0
  for mode in [Mode.lease, Mode.ffOnly] do
    let task := s!"trace/{mode.label}"
    for ev in trace do
      let c := who[actorOf ev]!
      let s ← c.read task
      x.tally.check ((← c.submit mode task s ev) matches .won _) s!"trace {mode.label}: {repr ev}"
      pushes := pushes + 1
    let s ← x.r.read task
    x.tally.check (s.val == ⟨.done, 9, none, some 8, some 2, 2⟩) s!"trace {mode.label}: final status"
    x.tally.check ((← x.r.audit s.rev.get!).isEmpty) s!"trace {mode.label}: 20 commits form a legal run"
  -- zombie: w1 holds a lease, the admin expires it, w1 comes back
  let task := "trace/zombie"
  let admin := who[0]!
  let w1 := who[1]!
  let w2 := who[2]!
  let s₀ ← w1.read task
  x.tally.check ((← w1.submit .lease task s₀ (.claim 1 0)) matches .won _) "zombie: w1 claims"
  let held ← w1.read task
  let seen ← admin.read task
  x.tally.check ((← admin.submit .lease task seen (.expire 1)) matches .won _) "zombie: admin expires w1"
  -- w1 still believes it holds the lease and reports on its old snapshot: git rejects
  x.tally.check ((← w1.submit .lease task held (.report 1 1 (.success 7))) == .lost)
    "zombie: report on the pre-expiry snapshot is rejected by git (lease mode)"
  x.tally.check ((← w1.submit .ffOnly task held (.report 1 1 (.success 7))) == .lost)
    "zombie: report on the pre-expiry snapshot is rejected by git (ff-only mode)"
  -- w1 re-reads: now the rule itself denies its old token
  let s₁ ← w1.read task
  x.tally.check ((← w1.submit .lease task s₁ (.report 1 1 (.success 7))) == .denied)
    "zombie: after re-reading, the old token is denied by the rules"
  -- another worker takes over and finishes; w1's old token stays dead
  let v ← w2.read task
  x.tally.check ((← w2.submit .lease task v (.claim 2 1)) matches .won _) "zombie: w2 claims"
  let s₂ ← w1.read task
  x.tally.check ((← w1.submit .lease task s₂ (.report 1 1 (.success 7))) == .denied)
    "zombie: w1's old token is denied while w2 holds the lease"
  let v ← w2.read task
  x.tally.check ((← w2.submit .lease task v (.report 2 2 (.success 7))) matches .won _) "zombie: w2 reports"
  -- the author cannot review its own PR
  let v ← w2.read task
  x.tally.check ((← w2.submit .lease task v (.claim 2 2)) == .denied) "zombie: author may not claim the review"
  x.tally.check (v.val == ⟨.review, 2, none, some 7, some 2, 1⟩) "zombie: final status"
  x.tally.check ((← w2.audit v.rev.get!).isEmpty) "zombie: history is a legal run"
  return pushes

def cleanup (x : Ctx) : IO Unit := do
  if !x.isLocal then
    let tips ← x.a.tips
    let mut batch : Array Upd := #[]
    let mut deleted := 0
    for (task, _) in tips do
      batch := batch.push ⟨x.a.ref task, none, none⟩
      if batch.size == 100 then
        deleted := deleted + ((← x.a.push .ffOnly batch).filter id).size
        batch := #[]
    deleted := deleted + ((← x.a.push .ffOnly batch).filter id).size
    let left ← x.a.tips
    IO.println s!"cleanup: deleted {deleted} test branches under {x.pfx}; {left.size} left"
  IO.FS.removeDirAll x.root

/-- A throwaway bare repository, configured like the production ruleset on `task-lock/**`. -/
def localRemote (root : System.FilePath) : IO String := do
  let dir := root / "remote.git"
  IO.FS.createDirAll dir
  for args in [#["init", "-q", "--bare", "."],
               #["config", "receive.denyNonFastForwards", "true"],
               #["config", "receive.denyDeletes", "true"]] do
    let out ← IO.Process.output { cmd := "git", args := args, cwd := some dir }
    if out.exitCode != 0 then throw <| IO.userError out.stderr
  return dir.toString

/-- Run the whole suite against one remote. Returns (passed, failed). -/
def suite (remote : Option String) : IO (Nat × Nat) := do
  let tally : Tally := ⟨← IO.mkRef 0, ← IO.mkRef 0⟩
  let root ← IO.FS.createTempDir
  let isLocal := remote.isNone
  let (url, pfx) ← match remote with
    | some url => pure (url, s!"task-lock-test/{(← IO.monoNanosNow) % 1000000000}/")
    | none => pure (← localRemote root, "task-lock/")
  let mk (name : String) := Client.create name root url pfx
  let x : Ctx := { tally, root, remote := url, pfx, isLocal,
                   a := ← mk "a", b := ← mk "b", r := ← mk "reader" }
  IO.println s!"\n== {if isLocal then "local" else "remote"}: {url}"
  IO.println s!"prefix: {pfx}"
  let t₀ ← IO.monoMsNow
  try
    let seeds ← buildSeeds x
    x.b.fetchAll
    let cases := allCases seeds
    if isLocal then printMatrix seeds
    let (nAcc, nDen) ← ruleMatrix x cases
    IO.println s!"1. transitions:       {nAcc} accepted pairs landed, in each of 2 push modes"
    IO.println s!"2. rule denials:      {nDen} denied pairs: honest client wrote nothing; forced through, the audit flagged each"
    let nStale ← staleMatrix x cases
    IO.println s!"3. git denials:       {nStale} stale writes rejected by git (every legal follow-up × 2 push modes)"
    refDenials x seeds
    IO.println s!"4. ref denials:       create-only, wrong expected tip, unrelated history"
    let nRace ← liveRace x (if isLocal then 8 else 6) (if isLocal then 5 else 2)
    IO.println s!"5. concurrent claims: {nRace} races, exactly one winner each"
    let nPush ← endToEnd x
    IO.println s!"6. end to end:        {nPush} single-event pushes, plus the zombie scenario"
  catch e =>
    tally.check false s!"exception: {e}"
  try cleanup x catch e => tally.check false s!"cleanup: {e}"
  let pass ← tally.pass.get
  let fail ← tally.fail.get
  IO.println s!"{pass} checks passed, {fail} failed, {((← IO.monoMsNow) - t₀) / 1000}s"
  return (pass, fail)

structure Opts where
  remote     : Option String := none
  remoteOnly : Bool := false

def parseArgs : List String → Opts → Except String Opts
  | [], o => .ok o
  | "--remote" :: url :: rest, o => parseArgs rest { o with remote := some url }
  | "--remote-only" :: rest, o => parseArgs rest { o with remoteOnly := true }
  | arg :: _, _ => .error s!"unknown argument: {arg}\nusage: git_backend_test [--remote URL] [--remote-only]"

def main (args : List String) : IO UInt32 := do
  let env ← IO.getEnv "AFE_FLEET_TEST_REMOTE"
  let env := env.filter (!·.isEmpty)
  let o ← match parseArgs args { remote := env } with
    | .ok o => pure o
    | .error e => IO.eprintln e; return 2
  if o.remoteOnly && o.remote.isNone then
    IO.eprintln "--remote-only needs --remote URL or AFE_FLEET_TEST_REMOTE"
    return 2
  let mut runs : Array (String × Nat × Nat) := #[]
  if !o.remoteOnly then
    runs := runs.push ("local", ← suite none)
  match o.remote with
  | some url => runs := runs.push ("remote", ← suite (some url))
  | none => IO.println "\n== remote: skipped (pass --remote URL or set AFE_FLEET_TEST_REMOTE to run it)"
  IO.println ""
  for (name, pass, fail) in runs do
    IO.println s!"{name}: {pass} passed, {fail} failed"
  return if runs.all (·.2.2 == 0) then 0 else 1
