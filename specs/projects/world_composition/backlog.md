---
status: open
---

# Backlog: World Composition

Items found while building composition that are out of scope for the phase that found them. Each is
closed or dismissed through the standard phase flow.

## Open

- **`AliasPath` makes the tool list disagree with validation.**
  `Field(validation_alias=AliasPath("p", 0))` publishes the *parameter's own* name in the tool
  schema but validates only the nested shape, so the name the tool list advertises is one
  validation rejects — by reference, by name, and for an agent's own call alike. That is exactly
  what `tool.py`'s module docstring says cannot happen ("the tool list an agent reads and the
  validation a call passes cannot drift apart").

  Pre-existing, and not introduced by composition: `docs/authoring.md` documents only
  `Field(alias=...)`, and phase 3's `Tool.arguments` reads the published schema, so the typed call
  path is exactly as wrong as the by-name path and no more. Closing it means refusing `AliasPath`
  in `tool._field`, which is a change to the registration surface that no phase of this project
  names.

- **Both fixture readers follow symlinks.**
  Planting a node's state file in a fixture directory as a symlink to a database outside it, with
  `file_sha256` set to the *target's* digest, makes `verify()` hash the target and `_copy_fixture`
  copy it into the new instance: the instance comes up on the outside file's contents, and every
  check passes.

  Pre-existing, and not introduced by composition: `fixture.state_path` has had this property since
  `format_version: 1`, so closing it -- a `path.is_symlink()` refusal in `fixtures._verify_file` --
  changes pre-phase-4 behaviour for every world and not only composite ones. Phase 4's
  `NodeMeta.file` validator closes the *naming* half of the same threat (a `file` that spells its
  way out of the directory) and deliberately not this half.

  What makes it worth deciding rather than leaving: `instances._open_child` hardens the
  working-directory side against exactly this attack -- `O_NOFOLLOW`, an owner check, and
  `mkdirat` on a descriptor rather than a path -- while the fixtures side does not, so the two
  sides of one framework disagree about one threat.

- **`world._check_name` and `fixtures.check_id` accept a Windows drive-relative name.**
  `C:x` passes both on Linux: `_check_name` refuses both platforms' separators but not a drive
  letter, and `check_id` refuses only the running platform's (`name != Path(name).name`). A world
  or a fixture id authored on Linux under such a name is refused the moment the same artifact is
  read on Windows, which is the failure a portable artifact format exists to prevent.

  Pre-existing and in framework code (`world.py`, `fixtures.py`), not composition's. Found while
  validating `NodeMeta.file`, whose new check is strictly stricter than either of these -- it
  refuses a name that is not `PurePosixPath(value).name` *and* not `PureWindowsPath(value).name` --
  so the three name rules the framework applies to durable artifacts now disagree with each other.

## Closed

_None yet._
