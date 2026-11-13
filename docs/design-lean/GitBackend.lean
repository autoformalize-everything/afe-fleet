import Lean.Data.Json
import TaskMachine

/-!
# GitHub backend: the task state machine over plain `git push`

Everything here is one of four git operations against the lock repo:

* `ls-remote`                      — read branch tips
* `fetch`                          — get the commits behind them
* `push --force-with-lease=R:OLD`  — compare-and-swap (OLD empty = create only)
* `push` (no flags)                — fast-forward only; a CAS as long as history is append-only

A lock branch is a chain of commits. Each commit's tree is exactly one file, `status.json`
(here: the fields of `Coord.Task`), and its message records the event that produced it, so
the branch is its own audit log.
-/

open Lean

namespace Coord
deriving instance ToJson, FromJson for Need
deriving instance ToJson, FromJson for Task
deriving instance ToJson, FromJson for Outcome
deriving instance ToJson, FromJson for Verdict
deriving instance ToJson, FromJson for Event
instance : Inhabited Task := ⟨Task.init⟩
instance : Inhabited Event := ⟨.accept⟩
end Coord

namespace Git
open Coord

/-- One participant (a worker or the admin) with its own local object store. -/
structure Client where
  name   : String
  dir    : System.FilePath
  remote : String
  /-- Branch prefix, `task-lock/` in production. -/
  pfx    : String
  /-- Extra environment (e.g. `GIT_SSH_COMMAND`). -/
  env    : Array (String × Option String) := #[]
  deriving Inhabited

def Client.ref (c : Client) (task : String) : String := s!"refs/heads/{c.pfx}{task}"

def Client.run (c : Client) (args : Array String) (input : Option String := none) :
    IO IO.Process.Output :=
  IO.Process.output {
    cmd := "git", args := args, cwd := some c.dir,
    env := #[("GIT_AUTHOR_NAME", some c.name), ("GIT_AUTHOR_EMAIL", some "afe-fleet-test@invalid"),
             ("GIT_COMMITTER_NAME", some c.name), ("GIT_COMMITTER_EMAIL", some "afe-fleet-test@invalid"),
             ("GIT_TERMINAL_PROMPT", some "0")] ++ c.env } input

def Client.git (c : Client) (args : Array String) (input : Option String := none) : IO String := do
  let out ← c.run args input
  if out.exitCode != 0 then
    throw <| IO.userError s!"[{c.name}] git {args} exited {out.exitCode}\n{out.stderr}"
  return out.stdout.trimAscii.toString

def Client.create (name : String) (root : System.FilePath) (remote pfx : String)
    (env : Array (String × Option String) := #[]) : IO Client := do
  let dir := root / name
  IO.FS.createDirAll dir
  let c : Client := { name, dir, remote, pfx, env }
  discard <| c.git #["init", "-q", "--bare", "."]
  return c

def Client.nonce (c : Client) : IO String := return s!"{c.name}-{← IO.monoNanosNow}"

/-- Build (locally) a commit whose tree is exactly `status.json`. -/
def Client.commit (c : Client) (status : Task) (parent : Option String) (msg : Json) :
    IO String := do
  let blob ← c.git #["hash-object", "-w", "--stdin"] (some ((toJson status).pretty ++ "\n"))
  let tree ← c.git #["mktree"] (some s!"100644 blob {blob}\tstatus.json\n")
  let p := match parent with
    | some p => #["-p", p]
    | none   => #[]
  c.git (#["commit-tree", tree] ++ p ++ #["-m", msg.compress])

def Client.eventMsg (c : Client) (ev : Event) : IO Json :=
  return Json.mkObj [("event", toJson ev), ("by", c.name), ("nonce", (← c.nonce))]

def Client.publishMsg (c : Client) : IO Json :=
  return Json.mkObj [("publish", true), ("by", c.name), ("nonce", (← c.nonce))]

/-! ## The write primitive -/

inductive Mode where
  /-- `--force-with-lease=<ref>:<expect>`: explicit compare-and-swap. -/
  | lease
  /-- Plain push: accepted only if it is a fast-forward. -/
  | ffOnly
  /-- `--force`: what a misbehaving client would do. -/
  | force
  deriving BEq, Repr

def Mode.label : Mode → String
  | .lease => "lease" | .ffOnly => "ff-only" | .force => "force"

/-- One ref update. -/
structure Upd where
  ref    : String
  /-- The tip the writer saw. `none` = the branch must not exist yet. -/
  expect : Option String
  /-- `none` = delete the branch. -/
  commit : Option String
  deriving Inhabited

/-- Push a batch of independent ref updates in one round trip.
Returns, per update, whether git accepted it. -/
def Client.push (c : Client) (mode : Mode) (us : Array Upd) : IO (Array Bool) := do
  if us.isEmpty then return #[]
  let flags := match mode with
    | .lease  => us.map fun u => s!"--force-with-lease={u.ref}:{u.expect.getD ""}"
    | .ffOnly => #[]
    | .force  => #["--force"]
  let specs := us.map fun u => s!"{u.commit.getD ""}:{u.ref}"
  let out ← c.run (#["push", "--porcelain"] ++ flags ++ #[c.remote] ++ specs)
  -- porcelain: "<flag>\t<from>:<to>\t<summary>"; flag '!' = rejected
  let mut flagOf : Array (String × String) := #[]
  for line in out.stdout.splitOn "\n" do
    match line.splitOn "\t" with
    | flag :: spec :: _ =>
      match (spec.splitOn ":").getLast? with
      | some to => flagOf := flagOf.push (to, flag)
      | none => pure ()
    | _ => pure ()
  let mut res := #[]
  for u in us do
    match flagOf.find? (·.1 == u.ref) with
    | some (_, flag) => res := res.push (flag != "!")
    | none =>
      throw <| IO.userError
        s!"[{c.name}] push gave no status for {u.ref} (exit {out.exitCode})\n{out.stdout}\n{out.stderr}"
  return res

/-! ## Reads -/

/-- All branch tips under the prefix, straight from the remote: `(task, sha)`. -/
def Client.tips (c : Client) : IO (Array (String × String)) := do
  let out ← c.git #["ls-remote", c.remote, s!"refs/heads/{c.pfx}*"]
  let base := s!"refs/heads/{c.pfx}"
  let mut res := #[]
  for line in out.splitOn "\n" do
    match line.splitOn "\t" with
    | [sha, ref] => if ref.startsWith base then res := res.push ((ref.drop base.length).toString, sha)
    | _ => pure ()
  return res

def Client.tip (c : Client) (task : String) : IO (Option String) := do
  let out ← c.git #["ls-remote", c.remote, c.ref task]
  match (out.splitOn "\n").head!.splitOn "\t" with
  | [sha, _] => return some sha
  | _ => return none

/-- Fetch every lock branch in one round trip. -/
def Client.fetchAll (c : Client) : IO Unit :=
  discard <| c.git #["fetch", "-q", "--no-tags", "--prune", c.remote,
    s!"+refs/heads/{c.pfx}*:refs/remotes/lock/*"]

def Client.statusAt (c : Client) (sha : String) : IO Task := do
  let txt ← c.git #["cat-file", "-p", s!"{sha}:status.json"]
  match Json.parse txt >>= fromJson? with
  | .ok t => return t
  | .error e => throw <| IO.userError s!"bad status.json at {sha}: {e}"

structure Snap where
  rev : String
  val : Task
  deriving Inhabited

/-- `snapshot()` for one task. -/
def Client.read (c : Client) (task : String) : IO (Option Snap) := do
  let some _ ← c.tip task | return none
  discard <| c.git #["fetch", "-q", "--no-tags", c.remote, s!"+{c.ref task}:refs/remotes/lock/{task}"]
  let sha ← c.git #["rev-parse", s!"refs/remotes/lock/{task}"]
  return some ⟨sha, ← c.statusAt sha⟩

/-- `snapshot()` for every task: one fetch, then local reads. -/
def Client.snapshot (c : Client) : IO (Array (String × Snap)) := do
  c.fetchAll
  let out ← c.git #["for-each-ref", "--format=%(refname)\t%(objectname)", "refs/remotes/lock/"]
  let base := "refs/remotes/lock/"
  let mut res := #[]
  for line in out.splitOn "\n" do
    match line.splitOn "\t" with
    | [ref, sha] => res := res.push ((ref.drop base.length).toString, ⟨sha, ← c.statusAt sha⟩)
    | _ => pure ()
  return res

/-! ## `submit`: what a worker or the admin actually calls -/

inductive Res where
  | won (rev : String)
  /-- git rejected the push: someone else moved the branch first. -/
  | lost
  /-- `step` rejected the event; nothing was pushed. -/
  | denied
  deriving Repr, BEq

/-- One compare-and-swap, with the "unknown outcome → read back" rule. -/
partial def Client.cas (c : Client) (mode : Mode) (task : String) (expect : Option String)
    (commit : String) (tries : Nat := 20) : IO Bool := do
  let pushed ← try
      let r ← c.push mode #[⟨c.ref task, expect, some commit⟩]
      pure r[0]!
    catch _ => pure false
  if pushed then return true
  let tip ← c.tip task
  if tip == some commit then return true        -- it landed; only the reply was lost
  if tip != expect || tries == 0 then return false   -- someone else moved it
  IO.sleep 50                                   -- not applied and not moved: transient, retry
  c.cas mode task expect commit (tries - 1)

def Client.publish (c : Client) (task : String) : IO Res := do
  let commit ← c.commit Task.init none (← c.publishMsg)
  return if ← c.cas .lease task none commit then .won commit else .lost

def Client.submit (c : Client) (mode : Mode) (task : String) (s : Snap) (ev : Event) : IO Res := do
  match step s.val ev with
  | none => return .denied
  | some t' =>
    let commit ← c.commit t' (some s.rev) (← c.eventMsg ev)
    return if ← c.cas mode task (some s.rev) commit then .won commit else .lost

/-! ## Audit: is this branch a legal run of the state machine? -/

/-- Walk the branch from its root. Every commit must hold exactly `status.json`, have at most
one parent, and be `step parent event`. Returns the violations (empty = legal).
Needs the commits locally (`fetchAll` or `read` first). -/
def Client.audit (c : Client) (tip : String) : IO (Array String) := do
  let log ← c.git #["log", "--reverse", "--format=%H%x09%P%x09%s", tip]
  let mut bad := #[]
  let mut prev : Option Task := none
  let mut i := 0
  for line in log.splitOn "\n" do
    match line.splitOn "\t" with
    | [sha, parents, subject] =>
      let files ← c.git #["ls-tree", "--name-only", sha]
      if files != "status.json" then bad := bad.push s!"#{i} {sha.take 8}: tree is not exactly status.json"
      if (parents.splitOn " ").length > 1 then bad := bad.push s!"#{i} {sha.take 8}: merge commit"
      let st ← c.statusAt sha
      match prev, Json.parse subject with
      | none, .ok msg =>
        if (msg.getObjValAs? Bool "publish").toOption != some true || st != Task.init then
          bad := bad.push s!"#{i} {sha.take 8}: root is not a published task"
      | some p, .ok msg =>
        match msg.getObjValAs? Event "event" with
        | .ok ev =>
          if step p ev != some st then
            bad := bad.push s!"#{i} {sha.take 8}: not a legal step: {repr ev}"
        | .error _ => bad := bad.push s!"#{i} {sha.take 8}: no event in commit message"
      | _, .error _ => bad := bad.push s!"#{i} {sha.take 8}: commit message is not JSON"
      prev := some st
      i := i + 1
    | _ => bad := bad.push s!"unparseable log line: {line}"
  return bad

end Git
