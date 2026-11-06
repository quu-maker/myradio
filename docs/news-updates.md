# 好きなジャンルのニュースを更新する

この局は、`news-preferences.json` に設定したジャンルからニュース候補を集め、毎朝、日本語の台本案を作ります。初期設定は金融・経済、Steamの注目ゲーム、ゴルフ・バスケ、日本の主要ニュースです。ゴシップは扱わず、金融は確認できる事実を伝えます。台本は自動で公開せず、GitHub の Pull Request で内容と出典を確認してから公開します。

## ジャンルを設定する

`news-preferences.json` の `topics` に、読みたいジャンルと検索語を1項目ずつ書きます。

```json
{
  "name": "宇宙開発",
  "search_query": "宇宙開発 OR ロケット OR 衛星"
}
```

検索語は Google News の RSS 検索に使います。候補は設定したジャンルごとに集め、公開から36時間以内の記事を優先します。項目数は `articles_per_topic`、番組に載せる最大本数は `max_stories` で調整できます。

## 毎朝の更新を有効にする

自分の GitHub リポジトリにこのプロジェクトを置き、次の設定をします。

1. 自分の GitHub に空のリポジトリを作ります。
2. このフォルダーをそのリポジトリの `main` ブランチへ push します。今のローカル `upstream` は元の公開リポジトリを指すため、push 先には使わないでください。自分のリポジトリを `origin` として追加し、`main` を push します。

   ```sh
   git remote add origin https://github.com/OWNER/REPOSITORY.git
   git push -u origin main
   ```
3. GitHub の Settings → Secrets and variables → Actions で、`OPENAI_API_KEY` という Secret を作り、OpenAI API キーを登録します。
4. Settings → Actions → General で、GitHub Actions に Pull Request の作成を許可します。
5. Settings → Pages で、公開元を `gh-pages` ブランチにします。
6. `station.json` の `url` と `repo` を自分の公開先とリポジトリに合わせます。
7. Actions の `Daily news draft` を一度手動実行します。

ワークフローは毎日6時10分ごろ（日本時間）に動きます。GitHub Actions の混雑で開始が遅れることがあります。生成した台本は Pull Request に入り、マージされると音声と字幕タイミングを作って公開します。

Pull Request の作成が許可されていないと、日次処理は台本を作った後で止まります。Actions の設定後に再実行してください。

## ニュースの根拠と音声

ニュース候補には RSS の見出し、配信元、説明文、出典リンクを使います。記事本文全体は取り込まず、RSS や公開メタデータから分かる範囲で台本を作ります。内容と出典は公開前に確認してください。

音声には OpenAI Realtime API の日本語音声を使います。生成した音声と字幕タイミングは公開ブランチに置き、音声を `main` のGit履歴には残しません。API 利用料が発生します。

音声を作らず台本だけ確認したい場合は、`Daily news draft` の処理後に Pull Request をマージしないでください。
