# Independent BM25 evaluation: local preregistered run

Execution: **SUCCESS**; experiment: **NO-GO**.
Local source was uncommitted on the parent SHA below; see FREEZE_HISTORY.md for documentation-only source hash refresh.

Split: holdout; commit: `06038546450337a1c0e635649acf46894b36c267`; run: local.
Freeze SHA256: `acaf89de89487bb5ccbeff4735b1f725b2859dc0dd543126a78e6da6192efef8`.

Original Chinese +10pp check is retained separately; this report uses the preregistered opt-in plan.

| Scenario | N / groups | Dense Recall / nDCG | Hybrid Recall / nDCG | Recall delta [95% CI] | W/T/L |
|---|---:|---:|---:|---|---|
| all | 108 / 36 | 0.995 / 0.803 | 0.986 / 0.792 | -0.010 [-0.029, +0.000] | [0, 104, 1] |
| chinese | 36 / 36 | 1.000 / 0.757 | 1.000 / 0.777 | +0.000 [+0.000, +0.000] | [0, 35, 0] |
| code | 38 / 31 | 1.000 / 0.911 | 0.974 / 0.881 | -0.026 [-0.086, +0.000] | [0, 37, 1] |
| configuration | 9 / 9 | 1.000 / 0.725 | 1.000 / 0.698 | +0.000 [+0.000, +0.000] | [0, 9, 0] |
| decision | 2 / 2 | 1.000 / 0.715 | 1.000 / 0.693 | +0.000 [+0.000, +0.000] | [0, 2, 0] |
| english | 18 / 18 | 0.971 / 0.871 | 0.971 / 0.801 | +0.000 [+0.000, +0.000] | [0, 17, 0] |
| historical | 31 / 31 | 1.000 / 0.742 | 1.000 / 0.748 | +0.000 [+0.000, +0.000] | [0, 31, 0] |
| identifiers | 54 / 34 | 1.000 / 0.866 | 0.981 / 0.839 | -0.019 [-0.061, +0.000] | [0, 52, 1] |
| mixed | 54 / 36 | 1.000 / 0.811 | 0.981 / 0.799 | -0.019 [-0.059, +0.000] | [0, 52, 1] |
| multi_answer | 4 / 4 | 1.000 / 0.903 | 1.000 / 0.851 | +0.000 [+0.000, +0.000] | [0, 4, 0] |
| negation | 10 / 10 | 1.000 / 0.602 | 1.000 / 0.634 | +0.000 [+0.000, +0.000] | [0, 10, 0] |
| preference | 6 / 5 | 0.917 / 0.646 | 0.917 / 0.638 | +0.000 [+0.000, +0.000] | [0, 6, 0] |
| scope | 3 / 1 | 1.000 / 1.000 | 1.000 / 1.000 | +0.000 [+0.000, +0.000] | [0, 2, 0] |
| stale_fact | 30 / 30 | 1.000 / 0.691 | 1.000 / 0.711 | +0.000 [+0.000, +0.000] | [0, 30, 0] |

## Time and no-answer diagnostics

```json
{
  "stale_fact": {
    "stale_before": {
      "dense": 0.6666666666666666,
      "hybrid": 0.6333333333333333,
      "paired": {
        "delta": -0.03333333333333333,
        "ci95": [
          -0.16666666666666666,
          0.1
        ],
        "fact_groups": 30
      }
    },
    "current_present": {
      "dense": 1.0,
      "hybrid": 1.0,
      "paired": {
        "delta": 0.0,
        "ci95": [
          0.0,
          0.0
        ],
        "fact_groups": 30
      }
    }
  },
  "historical": {
    "historical_accuracy": {
      "dense": 0.5161290322580645,
      "hybrid": 0.5483870967741935,
      "paired": {
        "delta": 0.03225806451612903,
        "ci95": [
          -0.06451612903225806,
          0.12903225806451613
        ],
        "fact_groups": 31
      }
    }
  },
  "no_answer": {
    "interference": {
      "dense": 1.0,
      "hybrid": 1.0,
      "paired": {
        "delta": 0.0,
        "ci95": [
          0.0,
          0.0
        ],
        "fact_groups": 2
      }
    },
    "returned_count": {
      "dense": 5.666666666666667,
      "hybrid": 5.666666666666667,
      "paired": {
        "delta": 0,
        "ci95": [
          0.0,
          0.0
        ],
        "fact_groups": 2
      }
    }
  }
}
```

## Warm latency

| Timing / round | N pairs | Dense p50 / p95 ms | Hybrid p50 / p95 ms | p95 delta ms / % | ratio 95% CI |
|---|---:|---:|---:|---:|---|
| e2e / pooled | 324 | 88.8 / 120.5 | 90.4 / 114.2 | -6.3 / -5.2% | [0.9121812569140308, 1.1294487471384473] |
| e2e / 0 | 108 | 88.5 / 125.7 | 90.3 / 114.2 | -11.5 / -9.2% | [0.8402852884922727, 1.210915836025151] |
| e2e / 1 | 108 | 87.1 / 121.6 | 91.1 / 110.9 | -10.7 / -8.8% | [0.8435117504323566, 1.1669077261330854] |
| e2e / 2 | 108 | 89.4 / 108.3 | 87.5 / 117.4 | +9.1 / +8.4% | [0.9008785714921778, 1.2101848869184832] |
| retrieval / pooled | 324 | 46.9 / 50.9 | 47.9 / 52.4 | +1.5 / +3.0% | [1.0027963099939423, 1.0518409976955874] |
| retrieval / 0 | 108 | 46.9 / 50.0 | 48.0 / 52.4 | +2.5 / +5.0% | [1.0208685177861754, 1.0769523337801452] |
| retrieval / 1 | 108 | 46.7 / 51.4 | 47.6 / 52.8 | +1.4 / +2.8% | [0.9626946403062917, 1.081254687847459] |
| retrieval / 2 | 108 | 47.1 / 51.4 | 47.8 / 52.1 | +0.8 / +1.5% | [0.9951855471619921, 1.0518897510988707] |

## Cases to review

- `task-priority/q3` 星芒队列丢任务要 checkout 哪个 branch？ — 分支标识直接用于修复任务。
  Correct: ['task-priority/d']; conflict: []
  Dense: ['task-priority/a', 'task-priority/d', 'task-priority/b', 'task-priority/c', 'git-worktree/b', 'merge-decision/b', 'db-transaction/d', 'incident-evidence/b']
  Hybrid: ['task-priority/a', 'task-priority/b', 'task-priority/c', 'merge-decision/d', 'incident-evidence/b', 'git-worktree/a', 'retry-policy/a', 'cost-limit/d']
- `package-manager/q3` Which lockfile should Fenggang service reviews require now? — 审查要核对当前锁文件。
  Correct: ['package-manager/d']; conflict: ['package-manager/a']
  Dense: ['package-manager/d', 'package-manager/a', 'package-manager/b', 'upload-consent/b', 'review-tone/b', 'upload-consent/a', 'meeting-exception/d', 'encryption-key/d']
  Hybrid: ['package-manager/b', 'deployment-target/d', 'package-manager/d', 'package-manager/a', 'upload-consent/b', 'review-tone/b', 'upload-consent/a', 'meeting-exception/d']
- `compression-choice/q2` 紫苏旧快照采用哪种压缩级别？ — 旧 gzip 九级是历史正确。
  Correct: ['compression-choice/a']; conflict: ['compression-choice/b']
  Dense: ['compression-choice/a', 'compression-choice/b', 'compression-choice/c', 'compression-choice/d', 'cache-mode/c', 'embedding-model/a', 'embedding-model/b', 'encryption-key/a']
  Hybrid: ['compression-choice/d', 'compression-choice/c', 'compression-choice/a', 'compression-choice/b', 'tokenizer-policy/a', 'cache-mode/c', 'embedding-model/a', 'embedding-model/b']
- `import-evidence/q3` What file signature does Hehuan image import validate? — 图片格式题与 CSV 无关。
  Correct: ['import-evidence/c']; conflict: ['import-evidence/a']
  Dense: ['import-evidence/c', 'import-evidence/a', 'timeout-change/c', 'import-evidence/d', 'embedding-model/a', 'cost-limit/c', 'import-evidence/b', 'encryption-key/c']
  Hybrid: ['import-evidence/d', 'review-tone/d', 'import-evidence/c', 'import-evidence/a', 'timeout-change/c', 'embedding-model/a', 'cost-limit/c', 'import-evidence/b']
- `backup-policy/q1` 灯塔账簿现在 nightly backup 要打包什么？ — 九月新策略要求两份存储。
  Correct: ['backup-policy/b']; conflict: ['backup-policy/a']
  Dense: ['backup-policy/b', 'backup-policy/a', 'backup-policy/c', 'backup-policy/d', 'retention-period/c', 'db-transaction/a', 'encryption-key/c', 'db-transaction/c']
  Hybrid: ['backup-policy/a', 'backup-policy/b', 'backup-policy/c', 'backup-policy/d', 'db-transaction/b', 'cost-limit/b', 'document-format/b', 'vector-host/b']
- `git-worktree/q2` 溪石过去在哪里做集成验证？ — 历史共用工作区是所问事实。
  Correct: ['git-worktree/a']; conflict: ['git-worktree/b']
  Dense: ['git-worktree/a', 'git-worktree/c', 'git-worktree/d', 'git-worktree/b', 'network-address/a', 'network-address/b', 'meeting-exception/d', 'release-checklist/b']
  Hybrid: ['git-worktree/c', 'git-worktree/a', 'git-worktree/d', 'network-address/b', 'network-address/a', 'network-address/c', 'git-worktree/b', 'release-checklist/b']
- `retry-policy/q1` 赤羽 queue 收到 HTTP 409 现在还 retry 吗？ — 新规则只排除冲突码，其他错误不混同。
  Correct: ['retry-policy/b']; conflict: ['retry-policy/a']
  Dense: ['retry-policy/b', 'retry-policy/a', 'retry-policy/c', 'retry-policy/d', 'incident-evidence/a', 'db-transaction/d', 'incident-evidence/c', 'incident-evidence/b']
  Hybrid: ['retry-policy/a', 'retry-policy/b', 'retry-policy/d', 'issue-label/b', 'cost-limit/b', 'retry-policy/c', 'incident-evidence/a', 'network-address/b']
- `retry-policy/q2` 赤羽旧策略会重试哪些响应码？ — 旧三种码都要计入。
  Correct: ['retry-policy/a']; conflict: ['retry-policy/b']
  Dense: ['retry-policy/a', 'retry-policy/d', 'retry-policy/b', 'retry-policy/c', 'incident-evidence/a', 'db-transaction/d', 'cost-limit/d', 'cost-limit/b']
  Hybrid: ['retry-policy/d', 'retry-policy/a', 'retry-policy/c', 'retry-policy/b', 'incident-evidence/a', 'cache-mode/a', 'cache-mode/b', 'db-transaction/d']

Original Chinese +10pp point check: FAIL. This is not the warm-p95 decision.

## Decision details

```json
{
  "verdict": "NO-GO",
  "checks": [
    {
      "name": "mixed Recall gain strictly positive",
      "state": "FAIL",
      "delta": -0.018867924528301886,
      "ci95": [
        -0.058823529411764705,
        0.0
      ],
      "fact_groups": 35
    },
    {
      "name": "mixed point gain >=0.05",
      "state": "FAIL"
    },
    {
      "name": "chinese recall noninferiority",
      "state": "PASS",
      "delta": 0.0,
      "ci95": [
        0.0,
        0.0
      ],
      "fact_groups": 35
    },
    {
      "name": "chinese ndcg noninferiority",
      "state": "UNCERTAIN",
      "delta": 0.019837621200520957,
      "ci95": [
        -0.04768048716944223,
        0.08843822378074187
      ],
      "fact_groups": 35
    },
    {
      "name": "english recall noninferiority",
      "state": "PASS",
      "delta": 0.0,
      "ci95": [
        0.0,
        0.0
      ],
      "fact_groups": 17
    },
    {
      "name": "english ndcg noninferiority",
      "state": "UNCERTAIN",
      "delta": -0.06950472331124133,
      "ci95": [
        -0.16767043218950473,
        0.023906826167203892
      ],
      "fact_groups": 17
    },
    {
      "name": "code recall noninferiority",
      "state": "UNCERTAIN",
      "delta": -0.02631578947368421,
      "ci95": [
        -0.08571428571428572,
        0.0
      ],
      "fact_groups": 31
    },
    {
      "name": "code ndcg noninferiority",
      "state": "UNCERTAIN",
      "delta": -0.030377220777549563,
      "ci95": [
        -0.0897783084273337,
        0.025593466782737165
      ],
      "fact_groups": 31
    },
    {
      "name": "identifiers recall noninferiority",
      "state": "UNCERTAIN",
      "delta": -0.018867924528301886,
      "ci95": [
        -0.061224489795918366,
        0.0
      ],
      "fact_groups": 34
    },
    {
      "name": "identifiers ndcg noninferiority",
      "state": "UNCERTAIN",
      "delta": -0.026891118943401283,
      "ci95": [
        -0.0726320435216598,
        0.016258223777136337
      ],
      "fact_groups": 34
    },
    {
      "name": "stale-before-current regression",
      "state": "UNCERTAIN",
      "delta": -0.03333333333333333,
      "ci95": [
        -0.16666666666666666,
        0.1
      ],
      "fact_groups": 30
    },
    {
      "name": "current fact presence",
      "state": "PASS",
      "delta": 0.0,
      "ci95": [
        0.0,
        0.0
      ],
      "fact_groups": 30
    },
    {
      "name": "historical accuracy",
      "state": "UNCERTAIN",
      "delta": 0.03225806451612903,
      "ci95": [
        -0.06451612903225806,
        0.12903225806451613
      ],
      "fact_groups": 31
    },
    {
      "name": "warm E2E p95 pooled",
      "state": "PASS",
      "ci95": [
        0.9121812569140308,
        1.1294487471384473
      ]
    },
    {
      "name": "warm E2E p95 0",
      "state": "UNCERTAIN",
      "ci95": [
        0.8402852884922727,
        1.210915836025151
      ]
    },
    {
      "name": "warm E2E p95 1",
      "state": "PASS",
      "ci95": [
        0.8435117504323566,
        1.1669077261330854
      ]
    },
    {
      "name": "warm E2E p95 2",
      "state": "UNCERTAIN",
      "ci95": [
        0.9008785714921778,
        1.2101848869184832
      ]
    }
  ],
  "reasons": [
    "no-answer interference: insufficient independent fact groups"
  ]
}
```

Recommendation: Keep this Draft experiment isolated; resolve the reported uncertainty or regressions before proposing a Server-only default-off entry point.
