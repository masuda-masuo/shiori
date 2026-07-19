---
name: architecture-decision-record
description: Write design docs, issues, and PRs in ADR (Architecture Decision Record) format. Use when creating or updating design documents, filing issues with design implications, or opening PRs that involve architectural decisions.
---

# Architecture Decision Record (ADR)

Write design documents, issues, and PRs in structured ADR format so decisions are
searchable and traceable.

## テンプレート

```markdown
# [タイトル]

**Status:** [proposed | accepted | rejected | deprecated | superseded by ADR-NNN]
**Deciders:** [決定に関わった主体。エージェントのみ/人間のみ/両方をカンマ区切りで]
**Date:** [YYYY-MM-DD]

## Context

何がこの決定を必要としているのか。解決したい問題、現状の制約、関連する背景。
技術的な理由だけでなく、チームの状況やビジネス上の優先順位も含める。

## Decision Drivers

この決定を動かした要因（優先順位順）:
- [driver 1]
- [driver 2]

## Considered Options

検討した選択肢:
- [option 1] — 簡潔な説明
- [option 2] — 簡潔な説明

## Decision Outcome

選んだ選択肢: [option]

理由: [decision driver のどれを満たすか、他の選択肢と比べて何が優れているか]

### Positive Consequences

- これにより何が良くなるか

### Negative Consequences

- これにより失われるもの、注意すべきトレードオフ

## Pros and Cons of the Options

### [option 1]

- Good: [argument]
- Bad: [counterargument]

### [option 2]

- Good: [argument]
- Bad: [counterargument]
```

## 書き方の指針

### Context の書き方

- 組織の状況とビジネスの優先順位を説明する
- チームのスキルセットや社会的な要因も含める
- 判断に関係のあるメリット・デメリットを、自チームのニーズに即した言葉で書く

### Consequences の書き方

- この決定から導かれる結果、成果物、フォローアップを説明する
- 後続の ADR が必要になる場合がある（1つの大きな決定が細かい決定を連鎖的に生む）
- 1ヶ月後に ADR を見直す文化がある場合、そのプロセスにも触れる

### 一般原則

- **Rationale**: なぜその決定をしたかの理由を説明する。選択肢の比較、コスト/ベネフィットを含める
- **Specific**: 1 ADR は1つの決定について書く
- **Immutable**: 既存情報は変更しない。新しい情報を追加するか、新しい ADR で supersede する

### Deciders の書き方

Deciders で最も重要なのは **「誰が決めたか」ではなく「誰との相談で決めたか」**。
以下の3パターンを明確に区別する:

```markdown
**Deciders:** opencode                    # エージェントが独断。人間未確認。後で覆る可能性あり
**Deciders:** opencode, masuda             # 人間と相談して合意。人間の意図が反映されている
**Deciders:** masuda                       # 人間が自分で決定。エージェントは記録しただけ
```

- エージェント名単体（`opencode`）は「人間は見ていない」というwarning
- エージェント + 人間（`opencode, masuda`）は「合意済み」
- 人間名のみ（`masuda`）は「人間の意思決定」

この区別がないと、後から別のエージェントが `shiori_search` で発見したときに
「これは確定なのか、まだ仮決めなのか」が判断できない。

## いつ書くか

- **新規設計**: 最初から ADR として書き始める
- **設計を含む Issue**: 本文末に ADR 形式の Decision セクションを付ける
- **アーキテクチャ変更を含む PR**: PR description に ADR スニペットを含める
- **既存の決定の見直し**: 新しい ADR を作り `supersedes: ADR-NNN` と明示する

## Status ライフサイクル

```
proposed → accepted → (optional: deprecated → superseded by ADR-NNN)
                ↘ rejected
```

`supersedes` / `superseded by` でリンクを張ることで決定の連鎖が追跡可能になる。
