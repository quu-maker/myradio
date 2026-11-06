# 好きなジャンルのニュースを更新する

この局は、`news-preferences.json` に設定したジャンルからニュース候補を集め、毎朝、日本語の台本と音声を作って自動公開します。初期設定は金融・株式、Steamの注目ゲーム、ゴルフ・バスケ、日本の主要ニュースです。候補が十分にあれば15本前後を扱います。ニュース本文はRSSの見出しと公開概要を使い、COEIROINK:リリンちゃんの声をGitHub Actions上で合成します。OpenAI APIキーや音声APIの利用料は必要ありません。公開前の人手確認はないため、出典と内容を確認してください。

## ジャンルを設定する

`news-preferences.json` の `topics` に、読みたいジャンルと検索語を1項目ずつ書きます。

```json
{
  "name": "宇宙開発",
  "search_query": "宇宙開発 OR ロケット OR 衛星"
}
```

検索語は Google News の RSS 検索に使います。候補は設定したジャンルごとに集め、公開から36時間以内の記事を優先します。項目数は `articles_per_topic`、番組に載せる最大本数は `max_stories`、番組の長さは `target_duration_seconds` で調整できます。

## 毎朝の更新を有効にする

自分の GitHub リポジトリにこのプロジェクトを置き、次の設定をします。

1. 自分の GitHub に空のリポジトリを作ります。
2. このフォルダーをそのリポジトリの `main` ブランチへ push します。今のローカル `upstream` は元の公開リポジトリを指すため、push 先には使わないでください。自分のリポジトリを `origin` として追加し、`main` を push します。

   ```sh
   git remote add origin https://github.com/OWNER/REPOSITORY.git
   git push -u origin main
   ```
3. Settings → Pages で、公開元を `gh-pages` ブランチにします。
4. `station.json` の `url` と `repo` を自分の公開先とリポジトリに合わせます。
5. すぐに最初の回を作りたい場合は、Actions の `Daily news update` を手動実行します。通常は次の朝から自動で動きます。

ワークフローは毎日6時10分ごろ（日本時間）に動きます。GitHub Actions の混雑で開始が遅れることがあります。台本と音声、字幕タイミングを作り、`main` に保存してから GitHub Pages へ公開します。人がマージする作業はありません。

## ニュースの根拠と音声

ニュース候補には RSS の見出し、配信元、説明文、出典リンクを使います。記事本文全体は取り込まず、RSS や公開メタデータの範囲で読み上げます。生成AIによる言い換えや事実補足はしないため、概要が短い記事は読み上げも短くなります。自動公開のため、誤りや不適切な候補が混じる可能性があります。

音声はCOEIROINKのローカルエンジンでGitHub Actions上に合成します。エンジンとリリンちゃんの音声データはキャッシュに置き、生成した音声と字幕タイミングを公開ブランチへ送ります。音声データは `main` のGit履歴に入れず、OpenAIなどの外部音声APIキーや利用料も不要です。放送画面に `COEIROINK:リリンちゃん` と `CV:リリンちゃん@COEIROINK` を表示します。

公開済みの回を新しい声で作り直す場合は、Actions の `Publish approved news` を手動実行し、対象日と「公開済みの音声も新しい声で作り直す」を指定します。

手動で公開したい回がある場合は、`Publish approved news` ワークフローを使えます。
