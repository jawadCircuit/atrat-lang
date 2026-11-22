# Diagnostics

Atrat diagnostics are designed to be stable enough to recognize, with a source
location, a code, a plain-language explanation and a suggested next step.

```text
error[E0301]: borrow error: returned borrow must be derived from `x`
  --> app.atr:12:12
   |
12 |     return View { r: &local }
   |            ^^^^^^^^^^^^^^^^^
   = help: borrows of locals or temporaries cannot escape
```

## Codes

| Code | Meaning |
| --- | --- |
| `E0001` | Lexical error |
| `E0002` | Parse/syntax error |
| `E0201` | General type mismatch |
| `E0202` | Unknown name, type, field, trait or module |
| `E0203` | Non-exhaustive `match` |
| `E0300` | General borrow-checker error |
| `E0301` | Returned reference/view outlives its source |
| `E0302` | Move or assignment while borrowed |
| `E0303` | Use after move |
| `E0304` | Mutable/shared borrow conflict |
| `E0305` | Assignment to an immutable binding |
| `E0306` | Read/use before initialization |
| `E0307` | Borrow of a temporary |
| `E0308` | Reference/trait object stored where its lifetime is unsupported |
| `E0310` | Unsupported closure capture |
| `E0401` | Trait implementation/method conflict or missing requirement |
| `E0402` | Cross-file access to a private declaration or type |

Codes identify a category, not a guarantee that the wording will never improve.
The human-readable message, `help:` line and optional `note:` lines are part of
the diagnostic too. Notes can point at the earlier borrow that caused a
conflict:

```text
error[E0302]: borrow error: cannot assign to `x` while it is borrowed
  --> app.atr:4:5
   |
 4 |     x = 2
   |     ^
   = help: the borrow is still alive here
   = note: borrow of `x` starts here (app.atr:3)
```

## Principles

- Point at the offending expression, not only at the enclosing function.
- Mention the relevant earlier borrow line when one exists, with a mapped
  `= note:` location.
- Prefer a concrete repair: return an owned value, bind the owner first, use
  `var`, clone, or borrow instead of moving.
- Reject uncertain unsafe cases rather than accepting them silently.
