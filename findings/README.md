# Findings

What was measured while building and testing the copilot, and what changed because of it. Every number comes from a run kept in [evals/results/](../evals/results/) or from a measurement described here.

## Results at a glance

| Run | Data | Passed | Sentences backed by their rows | Other company's data shown | Cost |
|---|---|---|---|---|---|
| 2026-11-18 | The brief's 25 devices, 60 cases, each twice | 120 of 120 | 237 of 252 | 0 | 1.11 USD |
| 2026-11-24 | 10,025 devices, 120 cases | 117 of 120 | 796 of 803 | 0 | 2.10 USD |
| 2026-11-24 | 10,025 devices, the 6 software cases after the scorer fix | 6 of 6 | 63 of 63 | 0 | 0.16 USD |

"Sentences backed by their rows" is a second pass with the stronger model (`gpt-6.1-sol`) over every factual sentence, auditing the runtime judge (`gpt-6-luna`).

## 1. The first version broke at 10,000 devices

The read tools returned every matching device. On the generated fleet one question about laptops produced a tool result of 854,763 tokens, more than the model can read.

| Measure, on Harbor (5,000 devices) | Before | After |
|---|---|---|
| `find_devices` laptops only | 854,763 tokens | 5,053 tokens |
| `find_devices` low disk | 95,469 tokens | 5,083 tokens |
| Largest tool result | 854,763 tokens | 5,490 tokens |
| Time per tool call | | 14 to 240 ms |

The fix: list tools return one ranked page of 25 rows and a count row with the exact total and the breakdowns by model, OS, check or application over every match. The agent states the total from the count row and fetches another page only when asked. A script outside the app paged through every result for all six companies and compared the full device sets and the top-ranked device with the labels: 0 differences.

## 2. Loading 300,750 snapshots

Loading the large file takes 21.4 s and 1.9 GB. A profile put 43% of the time in Pydantic validation, 28% in JSON parsing and 15% in building the snapshot objects. The cost was accepted for a demo. A real fleet belongs in a database with an index per company and device.

## 3. The labels caught a rounding bug

The eval computes ground truth from the raw JSON without importing app code. On the first run the labels and the tools disagreed on one device: the tools compared rounded percentages, so 89.955% counted as 90% used. The tools now compare exact readings, and a test guards it.

## 4. The three misses on the large file were the scorer

The question "What software has been installed during the period, and where?" has two right totals. Each answer stated both, for example "353 new-software findings across 348 devices", and put the device total in its `count` field. The scorer expected installs. The scorer now accepts either, and the six software cases passed on a rerun. The answers were not changed.

## 5. Where the audit and the runtime judge disagreed

- On the brief's data, most disputed sentences said "over the last 30 days" while the cited count row gave no dates. The detector count rows now carry the date window. A rerun of the drift cases scored 16 of 16 sentences supported.
- On the large file, 7 of 803 were disputed: totals described as "in their latest snapshot" and counts of devices beyond the first page.

## 6. Repeatability

On the brief's data each case ran twice. 58 of 60 gave the same outcome and device list both times. The two that differed (`old_macos` for Globex and Initech) gave the right count both times and filled the device list in only one.

## 7. Bugs found by review and fixed

| Found | Fix |
|---|---|
| Approving one card of several rejected the others | The page waits until every card has a decision and sends them together |
| A failed run left tool calls without results, and the next model call failed | Unanswered tool calls get a placeholder result before the next call |
| A refused proposal's key blocked the corrected retry in the same turn | Only proposals that passed their rule, or actions already carried out, hold a key |
| Concurrent eval cases locked the shared SQLite file | Checkpoints and the audit log live in separate files |

The second and third have tests in [tests/](../tests/README.md).

## 8. Cost

The agent model costs twenty times the judge. A question costs about 1 to 2 cents with the agent, the judge and one rewrite at most. The audit pass is an eval cost and does not run in the app.
