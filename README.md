# m01-s64-o3: merge of two Arena agents

`harness.py` runs two complete agents on the same ICP, one after the other, each in its own child process
(`_merge_child.py`) with its own time window and spend view, and returns the best rows of both
(`merge_rows`: duplicates removed, stage-evidence rows first on staged ICPs, at most the company limit).
Settings: `merge.json`.

## Agents

- `agent_a/`: reserve: P s64 (5F44hJ9mEc, 6.68 on 10-22; cheapest precision-first P). Source: public Arena submission `sub-44da292092778ba09a5f625ca8357fe2` (arena-2026-10-22), unchanged except comments/docstrings stripped, test files removed, JSON data minified.
- `agent_b/`: P o3: 10-22 champion (5GTojcV63k, = lab 4e7ae4d); current baseline and duplicate reference. Source: public Arena submission `sub-e945a41e30bc07e9e42043ec27c0af8b` (arena-2026-10-22), unchanged except comments/docstrings stripped, test files removed, JSON data minified.

## Licence

The agents are licensed under the GNU Affero General Public License v3 (`LICENSE`); the Leadpoet platform
notice is in `LICENSE-leadpoet-platform.txt`. The merge harness is released under the same licence.
