# 游戏名称与 ID 对照

模型不再需要仅凭记忆猜中文昵称或翻译。运行时使用本地名称目录；本局 context 带入已知英雄与已观测强化的对照，词库之外的名称由 `resolve_game_name` 查询。它不使用 Google Search，不调用额外模型。

本次快照版本 16.17，包含 173 个英雄、267 个有 Mayhem 来源证据的强化。
强化范围取 KIWI 模式清单与同版本已缓存 OP.GG Mayhem ID 的并集，并记录证据来源。
模式清单本身不等于完整可选池；此目录也不承诺涵盖所有未来新增强化或玩家昵称。

## 数据来源

- 腾讯国服英雄目录：`https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js`。使用 heroId、中文正式名／称号、英文内部名和 keywords 中的昵称；例如“铁男”“火女”“猴子”。并不保证收录所有玩家黑话或语音识别错字。
- Riot Data Dragon：`https://ddragon.leagueoflegends.com/api/versions.json` 及指定版本的 `data/en_US/champion.json`。按数字英雄 ID 关联英文正式名，不能把内部名当成正式名：孙悟空是 Wukong，内部名是 MonkeyKing。
- CommunityDragon 已提取的客户端数据：同一版本的 `plugins/rcp-be-lol-game-data/global/{default|zh_cn|zh_my|zh_tw}/v1/cherry-augments.json` 与 `default/v1/augment-lists.json`。按正数 ID 和内部 API 名对齐英文、国服、马来西亚简中和台湾译名，使用模式证据区分 Arena 与海克斯大乱斗；不将同名 Arena 强化 ID 套到 Mayhem。本次没有修改或解包用户安装的客户端。
- 腾讯国服强化目录：`https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json`，由官方攻略站使用。按 `augmentID` 和 `name_en`（内部 API 名）交叉核对，排除 PBE 记录。当前 267 条中 233 条获得额外验证；另外 34 条仍有有效的客户端 `zh_CN` 国服译名，只是腾讯目录未覆盖。腾讯文件没有补丁版本字段，因此独立记录下载时间、哈希及交叉核对的客户端版本。
- 已缓存的 OP.GG 英雄目录用于校验工具 URL slug。例如孙悟空的工具 slug 是 monkeyking。OP.GG 强化 key 有时是图标名称，不能一律当作 API 名；数值 ID 与英文名称一致时才补充结果中的中文名称。

数据的版本、来源 URL、下载内容哈希、构建时间和目录哈希都保存在本地快照。游戏素材归 Riot 所有，CommunityDragon 的提取工具代码许可不能替代素材许可。

## 发给模型的内容

`VOICE_CONTEXT.nameGlossary` 只包含本局已知英雄和已观测强化；不会把整份目录每次塞进 prompt。包括中文名、英文名、已知昵称、内部标识、ID 和工具 slug。它不代表 Discord 用户与某个英雄绑定，也不代表知道玩家尚未选择的三个强化。

- “铁男” → 莫德凯撒 → Mordekaiser → championId 82 → mordekaiser。
- “猴子” → 孙悟空 → Wukong → championId 62 → MonkeyKing / monkeyking。
- “魔法飞弹” → Magic Missile → ARAM_MagicMissile → augmentId 1133。
- “掷骰狂人” → High Roller → ARAM_HighRoller → augmentId 2095。

强化记录的 `namesByLocale` 分别保存 `en_US`、`zh_CN`（中国大陆）、`zh_MY`（马来西亚）、`zh_TW`（台湾），267 条均具备四种名称。`nameZh` 保持指向 `zh_CN`；`namesByRegion.CN` 保存腾讯核对来源的名称，`regionVerification` 标注核对状态。三地翻译直接来自各自数据，不通过简繁转换推断。

| 英文 | 国服 zh_CN | 马来西亚 zh_MY | 台湾 zh_TW |
|---|---|---|---|
| High Roller | 掷骰狂人 | 豪掷千金 | 豪氣賭客 |
| Magic Missile | 魔法飞弹 | 魔法飞弹 | 魔法導彈 |
| Outlaw's Grit | 狂徒豪气 | 歹徒本色 | 俠盜恆毅 |
| Final City Transit | 最终都市列车 | 终末之城交通 | 終城快車 |

每一行通过同一 ID 与内部名称关联。任意地区名称都可用于本地解析和现有强化查询；context 与查询结果保留地区对照，prompt 要求沿用说话者使用的译名，除非用户要求转换地区。词库包含合法的纯标点名称，例如 `???`／`？？？`，但不同 ID 的重名仍返回歧义。

不在 context 的名字可调用 `resolve_game_name(kind="champion"|"augment", query=...)`。查询仅做规范化后的精确匹配，多个候选返回 ambiguous，未知返回 not_found。没有模糊匹配后默默选第一个。

现有查询工具也能接收已知中文英雄名／昵称和字符串数字 ID；中文强化 query 在本地解析为英文名后查询原有 OP.GG 数据。对不上词库时返回未解析信息，避免编造翻译。

## 更新与复现

在项目目录执行：

```powershell
python -m scripts.refresh_name_catalog
```

这是显式下载公共静态数据，不消耗模型 token。默认写入被 Git 忽略的 `logs/name_catalog/catalog.json`。任何所选强化缺少地区名称或出现跨语言 ID 错配，刷新会失败并保留上一份完整文件；context 与工具查询只读本地，更新后会重新加载本地快照，不在语音发送中联网取词库。

新增昵称／翻译能力的测试分三层：目录解析与冲突测试、真实 context/tool 路径的离线测试、固定工具结果的真实 Gemini 用例。文本用例通过仍不能保证有噪声的中文语音能正确识别，需要游戏录音／转写样本继续验证。
