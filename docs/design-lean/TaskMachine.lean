/-!
# Task coordination state machine

Core Lean 4 only (no Mathlib). Check with `lean TaskMachine.lean`.

Two layers:

* **Layer 1 — `step`**: the per-task state machine. A task is described by what it
  *needs next* (`work → review → merge → done`) plus an optional *lease*.
  There is no `failed` state: a failed attempt only releases the lease.
* **Layer 2 — `Log`**: the only thing a coordination backend has to provide — a
  per-task append-only log with compare-and-swap on its tip.
  GitHub backend: the lock branch `task-lock/<task-id>`, CAS = fast-forward push.
  Server backend: a row with a `rev` column, CAS = `UPDATE … WHERE rev = ?`.

What is proved (all names below are theorems in this file):

| property                                             | theorem                         |
|------------------------------------------------------|---------------------------------|
| invariant holds in every reachable state             | `reachable_wf`                  |
| a lost lease can never write again (zombie fencing)  | `zombie_fenced`                 |
| two racing claims: at most one wins                  | `claim_exclusive`               |
| retrying any event is rejected or a no-op            | `retry_safe`                    |
| a failed/expired attempt leaves the task claimable   | `failure_harmless`, `expire_harmless`, `work_always_claimable` |
| only a reviewer verdict or the admin can move a task backwards | `regress_only_by_verdict` |
| no dead ends: `done` is reachable from every state   | `no_dead_end`                   |
| nothing is ever stuck: some event is always enabled  | `never_stuck`                   |
| `done` is final                                      | `done_final`                    |
| a write decided on a stale snapshot is rejected      | `Log.stale_rejected`            |
| a successful write is a legal step of the *current* state | `Log.submit_is_step`       |
| racing writers on one snapshot: exactly one wins     | `Log.one_winner`                |
| every history either backend can produce is a legal run | `Log.run_reachable`, `Log.run_wf` |

Safety never mentions time. Lease expiry is the event `expire`; *when* the admin
fires it is policy (TTL). Firing it too early is wasteful but safe, because the
epoch fences the old holder (`zombie_fenced`).
-/

namespace Coord

abbrev Worker := Nat
abbrev PR := Nat

/-- What the task needs next. This is the whole "status" of a task. -/
inductive Need where
  | work    -- needs an author (first attempt, continuation of a partial, or fix after review)
  | review  -- has a PR; needs a reviewer
  | merge   -- approved; needs the admin to merge once CI is green
  | done    -- merged
  deriving DecidableEq, Repr

structure Task where
  need    : Need
  /-- Fencing token. Bumped by every claim; never decreases. -/
  epoch   : Nat
  /-- Current lease holder, if any. -/
  lease   : Option Worker
  /-- Latest PR for this task (kept across failed attempts, so work resumes from it). -/
  pr      : Option PR
  /-- Who produced the PR that is up for review. A reviewer must be someone else. -/
  author  : Option Worker
  /-- Leases that ended without progress. Drives backoff/escalation policy only. -/
  strikes : Nat
  deriving DecidableEq, Repr

/-- A task nobody has claimed yet. On GitHub: a task in the tree with no lock branch. -/
def Task.init : Task := ⟨.work, 0, none, none, none, 0⟩

/-- Final report of a work lease. -/
inductive Outcome where
  | success (pr : PR)   -- PR is ready for review
  | part    (pr : PR)   -- "partial": some progress, pushed as a draft PR; task stays in `work`
  | failure             -- nothing usable; debug info goes in the payload
  deriving DecidableEq, Repr

/-- Final report of a review lease. -/
inductive Verdict where
  | approve
  | changes   -- review comment left on the PR; task goes back to `work`
  | abstain   -- reviewer could not finish; task stays in `review`
  deriving DecidableEq, Repr

inductive Event where
  /-- Worker `w` saw epoch `e` with no lease and takes it. Its token is `e + 1`. -/
  | claim     (w : Worker) (e : Nat)
  /-- Lease holder is alive. Changes nothing; *rejection* tells a worker it was fenced. -/
  | heartbeat (w : Worker) (e : Nat)
  | report    (w : Worker) (e : Nat) (o : Outcome)
  | review    (w : Worker) (e : Nat) (v : Verdict)
  /-- Admin: the lease with token `e` is considered dead. -/
  | expire    (e : Nat)
  /-- Admin: the PR was merged. -/
  | accept
  /-- Admin: CI red, merge conflict, PR closed, or head moved after approval. -/
  | rework
  deriving DecidableEq, Repr

def Task.claimable (t : Task) (w : Worker) : Prop :=
  match t.need with
  | .work   => True
  | .review => t.author ≠ some w
  | _       => False

instance (t : Task) (w : Worker) : Decidable (t.claimable w) := by
  unfold Task.claimable; split <;> infer_instance

/-- The state machine. `none` = the event is rejected and nothing changes. -/
def step (t : Task) : Event → Option Task
  | .claim w e =>
    if t.lease = none ∧ t.epoch = e ∧ t.claimable w then
      some { t with lease := some w, epoch := e + 1 }
    else none
  | .heartbeat w e =>
    if t.lease = some w ∧ t.epoch = e then some t else none
  | .report w e o =>
    if t.lease = some w ∧ t.epoch = e ∧ t.need = .work then
      match o with
      | .success pr => some { t with lease := none, need := .review, pr := some pr, author := some w }
      | .part pr    => some { t with lease := none, pr := some pr }
      | .failure    => some { t with lease := none, strikes := t.strikes + 1 }
    else none
  | .review w e v =>
    if t.lease = some w ∧ t.epoch = e ∧ t.need = .review then
      match v with
      | .approve => some { t with lease := none, need := .merge }
      | .changes => some { t with lease := none, need := .work }
      | .abstain => some { t with lease := none }
    else none
  | .expire e =>
    if t.lease ≠ none ∧ t.epoch = e then
      some { t with lease := none, strikes := t.strikes + 1 }
    else none
  | .accept =>
    if t.need = .review ∨ t.need = .merge then some { t with need := .done, lease := none }
    else none
  | .rework =>
    if t.need = .review ∨ t.need = .merge then some { t with need := .work, lease := none }
    else none

/-- Run a sequence of events; `none` if any of them is rejected. -/
def run (t : Task) : List Event → Option Task
  | []        => some t
  | ev :: evs => (step t ev).bind (run · evs)

theorem run_cons_some {t t' : Task} {ev : Event} {evs : List Event}
    (h : run t (ev :: evs) = some t') : ∃ m, step t ev = some m ∧ run m evs = some t' := by
  simp only [run] at h
  cases hm : step t ev with
  | none => simp [hm] at h
  | some m => exact ⟨m, rfl, by simpa [hm] using h⟩

theorem run_append {t m t' : Task} {xs ys : List Event}
    (h₁ : run t xs = some m) (h₂ : run m ys = some t') : run t (xs ++ ys) = some t' := by
  induction xs generalizing t with
  | nil => simp [run] at h₁; subst h₁; simpa using h₂
  | cons x xs ih =>
    obtain ⟨a, ha, hr⟩ := run_cons_some h₁
    simp [run, ha, ih hr]

def Reachable (t : Task) : Prop := ∃ evs, run Task.init evs = some t

/-! ## Invariant -/

/-- Well-formedness: only `work`/`review` can be leased, and everything past `work` has a PR. -/
def WF (t : Task) : Prop :=
  (t.lease ≠ none → t.need = .work ∨ t.need = .review) ∧
  (t.need ≠ .work → t.pr ≠ none ∧ t.author ≠ none)

theorem init_wf : WF Task.init := by simp [WF, Task.init]

theorem step_wf {t t' : Task} {ev : Event} (h : WF t) (hs : step t ev = some t') : WF t' := by
  obtain ⟨h₁, h₂⟩ := h
  unfold step at hs
  repeat' split at hs
  all_goals first
    | contradiction
    | (injection hs with hs; subst hs
       cases hn : t.need <;> simp_all [WF, Task.claimable])

theorem run_wf {t t' : Task} {evs : List Event} (h : WF t) (hr : run t evs = some t') : WF t' := by
  induction evs generalizing t with
  | nil => simp [run] at hr; subst hr; exact h
  | cons ev evs ih =>
    obtain ⟨m, hm, hr⟩ := run_cons_some hr
    exact ih (step_wf h hm) hr

theorem reachable_wf {t : Task} (h : Reachable t) : WF t :=
  let ⟨_, hr⟩ := h; run_wf init_wf hr

/-! ## Fencing: a lost lease can never write again -/

theorem epoch_mono {t t' : Task} {ev : Event} (hs : step t ev = some t') : t.epoch ≤ t'.epoch := by
  unfold step at hs
  repeat' split at hs
  all_goals first
    | contradiction
    | (injection hs with hs; subst hs; simp_all)

/-- Token `e` is dead: either a later claim happened, or its lease was released. -/
def Dead (e : Nat) (t : Task) : Prop := e < t.epoch ∨ (t.epoch = e ∧ t.lease = none)

theorem dead_step {e : Nat} {t t' : Task} {ev : Event}
    (hd : Dead e t) (hs : step t ev = some t') : Dead e t' := by
  unfold Dead at *
  unfold step at hs
  repeat' split at hs
  all_goals first
    | contradiction
    | (injection hs with hs; subst hs; simp_all; try omega)

theorem dead_run {e : Nat} {t t' : Task} {evs : List Event}
    (hd : Dead e t) (hr : run t evs = some t') : Dead e t' := by
  induction evs generalizing t with
  | nil => simp [run] at hr; subst hr; exact hd
  | cons ev evs ih =>
    obtain ⟨m, hm, hr⟩ := run_cons_some hr
    exact ih (dead_step hd hm) hr

/-- A dead token is rejected by every lease-holder event. -/
theorem dead_rejects {e : Nat} {t : Task} (hd : Dead e t) (w : Worker) :
    step t (.heartbeat w e) = none ∧
    (∀ o, step t (.report w e o) = none) ∧
    (∀ v, step t (.review w e v) = none) := by
  unfold Dead at hd
  refine ⟨?_, ?_, ?_⟩
  · simp only [step]; split
    · rename_i h; rcases hd with hd | hd <;> simp_all <;> omega
    · rfl
  · intro o; simp only [step]; split
    · rename_i h; rcases hd with hd | hd <;> simp_all <;> omega
    · rfl
  · intro v; simp only [step]; split
    · rename_i h; rcases hd with hd | hd <;> simp_all <;> omega
    · rfl

/-- Every way of ending a lease kills its token. -/
theorem lease_end_dead {t t' : Task} {e : Nat} :
    (step t (.expire e) = some t' → Dead e t') ∧
    (∀ w o, step t (.report w e o) = some t' → Dead e t') ∧
    (∀ w v, step t (.review w e v) = some t' → Dead e t') := by
  refine ⟨?_, ?_, ?_⟩
  · intro hs; simp only [step] at hs; split at hs
    · injection hs with hs; subst hs; simp_all [Dead]
    · contradiction
  · intro w o hs; simp only [step] at hs
    repeat' split at hs
    all_goals first
      | contradiction
      | (injection hs with hs; subst hs; simp_all [Dead])
  · intro w v hs; simp only [step] at hs
    repeat' split at hs
    all_goals first
      | contradiction
      | (injection hs with hs; subst hs; simp_all [Dead])

/-- **Zombie fencing.** Once the admin expires the lease with token `e`, no matter what
happens afterwards, the old holder's heartbeat/report/review with that token is rejected.
So expiring a slow-but-alive worker is always safe. -/
theorem zombie_fenced {t t₁ t₂ : Task} {e : Nat} {evs : List Event}
    (hx : step t (.expire e) = some t₁) (hr : run t₁ evs = some t₂) (w : Worker) :
    step t₂ (.heartbeat w e) = none ∧
    (∀ o, step t₂ (.report w e o) = none) ∧
    (∀ v, step t₂ (.review w e v) = none) :=
  dead_rejects (dead_run (lease_end_dead.1 hx) hr) w

/-- **Mutual exclusion of claims.** Two workers that saw the same state race to claim:
whoever applies second is rejected. -/
theorem claim_exclusive {t t₁ : Task} {w₁ w₂ : Worker} {e : Nat}
    (h : step t (.claim w₁ e) = some t₁) : step t₁ (.claim w₂ e) = none := by
  simp only [step] at h; split at h
  · injection h with h; subst h; simp [step]
  · contradiction

/-- A claim based on a stale observation is rejected, forever. -/
theorem stale_claim_rejected {t : Task} {e : Nat} (h : e < t.epoch) (w : Worker) :
    step t (.claim w e) = none := by
  simp only [step]; split
  · rename_i hc; omega
  · rfl

/-! ## Retry safety -/

/-- **At-least-once delivery is safe.** If an event was applied and is then delivered again
(lost response, crash before journaling, duplicate request), the second delivery is either
rejected or changes nothing. No event can take effect twice. -/
theorem retry_safe {t t' : Task} {ev : Event} (hs : step t ev = some t') :
    step t' ev = none ∨ step t' ev = some t' := by
  unfold step at hs
  repeat' split at hs
  all_goals first
    | contradiction
    | (injection hs with hs; subst hs; simp_all [step])

/-! ## Tasks cannot fail -/

/-- A failed attempt changes nothing except releasing the lease and counting a strike. -/
theorem failure_harmless {t t' : Task} {w : Worker} {e : Nat}
    (hs : step t (.report w e .failure) = some t') :
    t'.need = .work ∧ t'.pr = t.pr ∧ t'.lease = none ∧ t'.epoch = t.epoch := by
  simp only [step] at hs; split at hs
  · injection hs with hs; subst hs; simp_all
  · contradiction

/-- A reaped lease changes nothing except releasing the lease and counting a strike. -/
theorem expire_harmless {t t' : Task} {e : Nat} (hs : step t (.expire e) = some t') :
    t'.need = t.need ∧ t'.pr = t.pr ∧ t'.author = t.author ∧ t'.lease = none := by
  simp only [step] at hs; split at hs
  · injection hs with hs; subst hs; simp_all
  · contradiction

/-- An unleased task that needs work can be claimed by anyone, whatever its history. -/
theorem work_always_claimable {t : Task} (hn : t.need = .work) (hl : t.lease = none)
    (w : Worker) : ∃ t', step t (.claim w t.epoch) = some t' := by
  simp [step, hn, hl, Task.claimable]

/-- An unleased task under review can be claimed by anyone but its author. -/
theorem review_claimable {t : Task} (hn : t.need = .review) (hl : t.lease = none)
    {w : Worker} (hw : t.author ≠ some w) : ∃ t', step t (.claim w t.epoch) = some t' := by
  simp [step, hn, hl, Task.claimable, hw]

def Need.rank : Need → Nat
  | .work => 3 | .review => 2 | .merge => 1 | .done => 0

/-- **No failure can move a task backwards.** The distance to `done` only ever grows by an
explicit judgement: a reviewer requesting changes, or the admin sending it back (`rework`).
Failures, partials, expiries and abstentions leave it where it was. -/
theorem regress_only_by_verdict {t t' : Task} {ev : Event} (hs : step t ev = some t') :
    t'.need.rank ≤ t.need.rank ∨ (∃ w e, ev = .review w e .changes) ∨ ev = .rework := by
  unfold step at hs
  repeat' split at hs
  all_goals first
    | contradiction
    | (injection hs with hs; subst hs; simp_all [Need.rank])

/-- `done` is final. -/
theorem done_final {t : Task} (h : WF t) (hd : t.need = .done) (ev : Event) :
    step t ev = none := by
  obtain ⟨h₁, _⟩ := h
  have hl : t.lease = none := by
    cases hl : t.lease with
    | none => rfl
    | some w => have := h₁ (by simp [hl]); simp_all
  cases ev <;> simp [step, hd, hl, Task.claimable]

/-! ## No dead ends -/

private theorem merge_to_done {t : Task} (hn : t.need = .merge) :
    ∃ t', run t [.accept] = some t' ∧ t'.need = .done := by
  simp [run, step, hn]

private theorem review_to_done {t : Task} (h : WF t) (hn : t.need = .review) :
    ∃ evs t', run t evs = some t' ∧ t'.need = .done := by
  obtain ⟨_, h₂⟩ := h
  obtain ⟨_, ha⟩ := h₂ (by simp [hn])
  cases hl : t.lease with
  | some w =>
    -- the current reviewer approves, the admin merges
    refine ⟨[.review w t.epoch .approve, .accept], ?_⟩
    simp [run, step, hn, hl]
  | none =>
    -- anyone but the author claims, approves, the admin merges
    cases hau : t.author with
    | none => exact absurd hau ha
    | some a =>
      refine ⟨[.claim (a + 1) t.epoch, .review (a + 1) (t.epoch + 1) .approve, .accept], ?_⟩
      simp [run, step, hn, hl, hau, Task.claimable]

/-- **No dead ends.** From every well-formed state — whatever mixture of failures, expiries,
rejected reviews and reworks led there — there is a continuation that reaches `done`
through the normal path (claim, report, review, merge). -/
theorem no_dead_end {t : Task} (h : WF t) :
    ∃ evs t', run t evs = some t' ∧ t'.need = .done := by
  cases hn : t.need with
  | done => exact ⟨[], t, rfl, hn⟩
  | merge => let ⟨t', h'⟩ := merge_to_done hn; exact ⟨_, t', h'⟩
  | review => exact review_to_done h hn
  | work =>
    -- first get to `review` with a PR, then reuse the review case
    have key : ∃ evs m, run t evs = some m ∧ m.need = .review := by
      cases hl : t.lease with
      | some w =>
        refine ⟨[.report w t.epoch (.success 0)], ?_⟩
        simp [run, step, hn, hl]
      | none =>
        refine ⟨[.claim 0 t.epoch, .report 0 (t.epoch + 1) (.success 0)], ?_⟩
        simp [run, step, hn, hl, Task.claimable]
    obtain ⟨evs₁, m, hr₁, hm⟩ := key
    obtain ⟨evs₂, t', hr₂, hd⟩ := review_to_done (run_wf h hr₁) hm
    exact ⟨evs₁ ++ evs₂, t', run_append hr₁ hr₂, hd⟩

/-- **Never stuck.** Every task that is not `done` has an enabled event. In particular a
leased task can always be released (`expire`), so no lock can be held forever. -/
theorem never_stuck {t : Task} (h : WF t) (hn : t.need ≠ .done) : ∃ ev t', step t ev = some t' := by
  obtain ⟨evs, t', hr, hd⟩ := no_dead_end h
  cases evs with
  | nil => simp [run] at hr; subst hr; exact absurd hd hn
  | cons ev evs => obtain ⟨m, hm, _⟩ := run_cons_some hr; exact ⟨ev, m, hm⟩

theorem lease_always_releasable {t : Task} (hl : t.lease ≠ none) :
    ∃ t', step t (.expire t.epoch) = some t' ∧ t'.lease = none := by
  simp [step, hl]

/-! ## Layer 2: the backend is a compare-and-swap log -/

/-- Per-task storage. GitHub: the commit chain of `task-lock/<task-id>` (each commit's tree is one
`status.json`). Server: the task row plus its rows in the `events` table. -/
structure Log where
  tip  : Task
  past : List Task   -- older statuses, newest first
  deriving Repr

/-- Revision of the tip. GitHub: the tip commit SHA. Server: the `rev` column. -/
def Log.rev (l : Log) : Nat := l.past.length

/-- What a client holds after reading. GitHub: `git fetch`. Server: `GET /tasks`. -/
structure Snapshot where
  rev : Nat
  val : Task

def Log.read (l : Log) : Snapshot := ⟨l.rev, l.tip⟩

/-- Compare-and-swap. GitHub: push a commit whose parent is `expect`, with
`--force-with-lease=<ref>:<expect>`. Server: `UPDATE tasks SET … WHERE id = ? AND rev = ?`. -/
def Log.cas (l : Log) (expect : Nat) (new : Task) : Option Log :=
  if l.rev = expect then some ⟨new, l.tip :: l.past⟩ else none

/-- The whole client protocol: decide on a snapshot, then CAS. -/
def Log.submit (l : Log) (s : Snapshot) (ev : Event) : Option Log :=
  match step s.val ev with
  | some t' => l.cas s.rev t'
  | none    => none

/-- `Extends l₀ l`: `l` is `l₀` after zero or more successful writes (by anyone). -/
inductive Extends : Log → Log → Prop where
  | refl (l : Log) : Extends l l
  | cas {l₀ l l' : Log} (expect : Nat) (new : Task) :
      Extends l₀ l → l.cas expect new = some l' → Extends l₀ l'

theorem Extends.rev {l₀ l : Log} (h : Extends l₀ l) : l₀.rev ≤ l.rev ∧ (l₀.rev = l.rev → l₀ = l) := by
  induction h with
  | refl => exact ⟨Nat.le_refl _, fun _ => rfl⟩
  | cas expect new _ hc ih =>
    unfold Log.cas at hc; split at hc
    · injection hc with hc; subst hc
      simp only [Log.rev, List.length_cons] at *
      exact ⟨by omega, fun h => by omega⟩
    · contradiction

/-- A write decided on an out-of-date snapshot is rejected. -/
theorem Log.stale_rejected {l : Log} {s : Snapshot} (h : l.rev ≠ s.rev) (ev : Event) :
    l.submit s ev = none := by
  unfold Log.submit; split
  · simp [Log.cas, h]
  · rfl

/-- **Linearizability.** A client reads `l₀`, other clients write (so the log is now `l`),
and then the client's write succeeds. Then nobody wrote in between after all, and the new
tip is a legal `step` of the *current* tip. -/
theorem Log.submit_is_step {l₀ l l' : Log} {ev : Event}
    (hx : Extends l₀ l) (hs : l.submit l₀.read ev = some l') :
    step l.tip ev = some l'.tip ∧ l'.past = l.tip :: l.past := by
  unfold Log.submit at hs; split at hs
  · rename_i t' ht
    unfold Log.cas at hs; split at hs
    · rename_i hrev
      injection hs with hs; subst hs
      have : l₀ = l := hx.rev.2 (by simpa [Log.read] using hrev.symm)
      subst this
      exact ⟨by simpa [Log.read] using ht, rfl⟩
    · contradiction
  · contradiction

/-- **One winner.** Two clients hold the same snapshot; if one write lands, the other is
rejected and must re-read. -/
theorem Log.one_winner {l l' : Log} {s : Snapshot} {ev₁ : Event}
    (h : l.submit s ev₁ = some l') (ev₂ : Event) : l'.submit s ev₂ = none := by
  unfold Log.submit at h; split at h
  · unfold Log.cas at h; split at h
    · rename_i hrev
      injection h with h; subst h
      exact Log.stale_rejected (by simp [Log.rev] at *; omega) ev₂
    · contradiction
  · contradiction

/-- Every history a backend can contain: start from an unclaimed task (on GitHub: no lock
branch, revision 0; the first successful submit creates it), then any number of clients
submit, each working from some earlier read of the log. -/
inductive Log.Run : Log → Prop where
  | init : Log.Run ⟨Task.init, []⟩
  | submit {l₀ l l' : Log} (ev : Event) :
      Log.Run l → Extends l₀ l → l.submit l₀.read ev = some l' → Log.Run l'

/-- **Refinement.** Whatever the interleaving of clients, the tip of the log is a state
reachable by the Layer-1 machine. So every Layer-1 theorem holds on both backends. -/
theorem Log.run_reachable {l : Log} (h : Log.Run l) : Reachable l.tip := by
  induction h with
  | init => exact ⟨[], rfl⟩
  | submit ev _ hx hs ih =>
    obtain ⟨evs, hr⟩ := ih
    have hstep := (Log.submit_is_step hx hs).1
    exact ⟨evs ++ [ev], run_append hr (by simp [run, hstep])⟩

theorem Log.run_wf {l : Log} (h : Log.Run l) : WF l.tip := reachable_wf (Log.run_reachable h)

/-! ## Worked traces -/

/-- Happy path: w1 authors PR 7, w2 approves, admin merges. -/
example : (run Task.init
    [.claim 1 0, .heartbeat 1 1, .report 1 1 (.success 7),
     .claim 2 1, .review 2 2 .approve, .accept]).map (·.need) = some .done := by decide

/-- Nothing fails: w1 dies, w2 makes partial progress (draft PR 7), w3 fails, w1 finishes on
PR 8, w1 may not review its own PR, w2 requests changes, w3 fixes, w1 approves, CI is red so
the admin sends it back, w2 fixes, w3 approves, admin merges. -/
example : (run Task.init
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
     .accept]).map (·.need) = some .done := by decide

/-- The author cannot review their own PR. -/
example : run Task.init [.claim 1 0, .report 1 1 (.success 7), .claim 1 1] = none := by decide

/-- A zombie (w1, expired at token 1) cannot report, even though it still "thinks" it holds
the lease, and even after the same worker id re-claims the task. -/
example : run Task.init [.claim 1 0, .expire 1, .claim 1 1, .report 1 1 (.success 7)] = none := by
  decide

end Coord
