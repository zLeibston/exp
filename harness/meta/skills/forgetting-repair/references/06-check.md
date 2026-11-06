# 06 验证与小修正

Validate the changed candidate Task Harness: check affected interfaces, imports,
call signatures and Python syntax. Where practical, exercise the repaired local
behavior with a small fixture or stubbed model, including a successful case that
should remain intact.

If a check exposes a problem, make the smallest necessary correction and rerun
the affected check. Keep corrections within the repair's scope and preserve
existing useful behavior. Stop when the relevant checks pass; do not expand
into unrelated changes.

Use checks available in the current container. Local validation does not establish
benchmark recovery; repeated task evaluation remains with the outer loop.
