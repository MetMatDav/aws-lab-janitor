# 0014. Three kinds of tests: examples, adversaries and properties

## Status

Accepted on 2026-11-12, before deletions were enabled.

## Context

The suite had about seven hundred tests, all of one kind: an input chosen by
the author, and the output the author expected. Such tests pin what the author
thought of. A program that deletes resources fails on what the author did not
think of. Two reviews were run before the cutover, by two agents with separate
contexts: one read the tests against property-based testing, the other tried to
break the code.

## Decision

The suite keeps three kinds of tests, each asking a different question.
Example tests (unit tests against recorded AWS responses, and assertions on the
synthesized template) ask "does it do what I meant". Adversarial tests ask
"what can someone else make it do", from three footholds: seeing an approval
URL, being able to tag resources, being able to start executions or write the
plans bucket. Property tests ask "does this hold for every input": generated
inputs, and an invariant instead of an expected value. A defect that is known
and not fixed stays in the suite as a test that fails on purpose, so fixing it
cannot go unnoticed.

## Consequences

- The adversarial pass found no way to delete something outside an approved,
  re-verified plan, and fifteen other defects; twelve were fixed and three are
  [known limitations](../known-limitations.md).
- Random inputs found what examples had not: plans whose hash depended on the
  order of their findings, a confirmation code that accepted look-alikes, a
  number DynamoDB would have stored as negative infinity.
- The property tests are checked themselves: breaking the code on purpose (the
  executor ignoring the keep tag, an expired approval being claimable) makes
  them fail.
- Property tests are slower and can fail on an input never seen before. CI
  runs them derandomized so that a red build can be reproduced.

## Where to look

- [tests/adversarial](../../tests/adversarial) and
  [tests/property](../../tests/property).
- [tests/property/test_approval_state_machine.py](../../tests/property/test_approval_state_machine.py):
  `ApprovalStoreMachine`.
- [tests/property/test_verify_props.py](../../tests/property/test_verify_props.py):
  `test_detector_and_recheck_agree`.
- [docs/testing.md](../testing.md).
