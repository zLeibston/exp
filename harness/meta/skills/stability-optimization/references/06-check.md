# 06 行为检查与总结

Review the candidate change against the diagnosed failure and known successful
behavior. Check affected imports, call signatures, tool schemas, return formats
and entry interfaces where relevant. Parse changed Python files without writing
bytecode into read-only source.

Exercise the local mechanism when feasible: a pure function, a recorded tool
response with a stubbed model, or a small fixture reproducing the relevant
condition. Include an ordinary successful case and a boundary/failure case.
If checks expose a defect, make the smallest correction and rerun the affected
checks. Do not expand into unrelated refactoring while fixing the check.

Revisit the cross-task evidence for conflicts and review the final candidate diff.
State which checks ran and which could not be performed. This Meta view does not
supply task environments or hidden tests; do not make paid model calls or claim
benchmark reruns from static/local checks. Repeated evaluation belongs to the
outer loop.

Keep a brief record of the change, checks and remaining uncertainty for the final response.
