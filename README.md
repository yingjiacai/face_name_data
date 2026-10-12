# Wikidata 名字—P18 图像采集

下载带有 P18 的 Wikidata 条目及其 Commons 图片，供后续数据建设使用。默认覆盖所有实体类别；人物、地点、作品等都会保留。`--scope humans` 可限定为具有非弃用 `P31=Q5` 声明的条目。

图片默认使用 **960 像素宽的 Commons 缩略图**，不在本地放大或重新压缩。原图较小时可能返回原图尺寸；纵向图片的高度可能大于 960。需要更省空间时首次下载可设 `--width 500`。每个输出目录固定一个分辨率配置。

## 工程方案与采集语义

1. `sample` 获取少量在线条目，验证网络和目录；`import-dump` 从官方完整 JSON dump 逐行提取全量清单。全量采集不依赖公开 SPARQL 服务的分页或大型查询。
2. 默认收集 Q/P 条目的全部非弃用 P18 声明，包括 normal 和 preferred；忽略 deprecated、novalue、somevalue。保留声明 ID、rank、限定符和引用。此处“有效声明”只表示结构上有值且未弃用，不表示图像正确。
3. 姓名优先中文、简体中文、英文，缺少时取其余可用 label；完全没有 label 时用 QID 并明确标记。保留所有语言的 labels、aliases、descriptions，及 P31 类型、P373 分类、Wikidata 版本号。
4. 通过 Commons `imageinfo` 获取缩略图地址、原图地址、原始尺寸、作者和许可等源信息。一个实体多张 P18 全部保留；多个实体共享同一规范化文件名时，只下载一次文件。
5. SQLite 保存实体、对应关系、下载状态和 dump 进度；文件先写 `.part`，通过实际图像解码和 SHA-256 计算后再原子重命名。重跑可跳过已完成文件，失败任务可单独重试。
6. 下载阶段不做人脸检测或身份验证。所有导出记录的 `reference_verified` 都是 `false`；不得直接把下载完成当成已经确认的 GT。

Python 3.10+，Linux/macOS。CPU 机器即可。元数据 API 串行请求，图片默认 2 个工作线程、总请求起始频率最多约 2 次/秒；429、5xx、`maxlag` 会退避，遵守 `Retry-After`。增加 CPU 核数不会消除源站的限速。

## 安装与小样本

以下命令在 Google Cloud 机器的仓库目录运行。数据目录应位于容量足够的持久磁盘；本机无需先下载数据再上传。建议用 `tmux` 保持会话。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 改成你的真实联系邮箱或公开项目/用户页；无需 API Key。
export P18_USER_AGENT='WikidataP18Downloader/0.1 (mailto:YOUR_REAL_EMAIL)'

python scripts/download_p18.py sample --output data/p18-pilot --limit 200
python scripts/download_p18.py download --output data/p18-pilot
python scripts/download_p18.py export --output data/p18-pilot
```

也可以指定条目，例如人物、地点和历史人物混合检查：

```bash
python scripts/download_p18.py sample --output data/p18-qids --qids Q42 Q90 Q517
python scripts/download_p18.py download --output data/p18-qids
python scripts/download_p18.py export --output data/p18-qids
```

`sample` 默认范围也是 `all`，上限 1000。它是在线查询返回的前一批结果，不是随机样本，更不代表完整数据集。第一次取到的 ID 列表会保存，恢复时不重新选样。在线数据可能在查询与取条目之间变更；不再具有 P18 的条目会记入日志。

## 全量：在云端直接下载官方 dump

先从[官方目录](https://dumps.wikimedia.org/wikidatawiki/entities/)选一份**带日期的完整 JSON dump**，不要用持续变化的 `latest` 链接做跨日断点续传。下面是 2026-10-12 核验过的 2026-10-05 快照示例，后续旧快照可能被轮换清理。`.bz2` 压缩包约 103 GB，`.gz` 约 156 GB；gzip 通常解压更快，bz2 更节省暂存空间。除此之外还要为图片、SQLite 和 JSONL 预留空间，图片总量应先以小样本估算。

在 `tmux` 中逐条执行；每一步成功后再执行下一步：

```bash
mkdir -p downloads
curl --fail --location --retry 8 --retry-delay 15 --continue-at - \
  --output downloads/wikidata-20261005-all.json.bz2.part \
  https://dumps.wikimedia.org/wikidatawiki/entities/20261005/wikidata-20261005-all.json.bz2
```

**只有 curl 成功退出后**才把 `.part` 重命名。断网时重复上一条命令即可续传，不要先重命名不完整文件。

```bash
mv downloads/wikidata-20261005-all.json.bz2.part downloads/wikidata-20261005-all.json.bz2
curl --fail --location \
  --output downloads/wikidata-20261005-sha1sums.txt \
  https://dumps.wikimedia.org/wikidatawiki/entities/20261005/wikidata-20261005-sha1sums.txt

# Google Cloud Linux；只检查本次下载的文件。
cd downloads
awk '$2 == "wikidata-20261005-all.json.bz2"' wikidata-20261005-sha1sums.txt | sha1sum --check -
cd ..
```

校验通过后建立全量清单，再下载图像：

```bash
python scripts/download_p18.py import-dump \
  --dump downloads/wikidata-20261005-all.json.bz2 --output data/p18-all

# 先下载全量清单中的 200 个唯一图片，确认权限和空间。
python scripts/download_p18.py download --output data/p18-all --limit 200
python scripts/download_p18.py export --output data/p18-all

# 去掉 limit 后继续全部下载，已完成文件会跳过。
python scripts/download_p18.py download --output data/p18-all
python scripts/download_p18.py export --output data/p18-all
```

首轮导入读完整个压缩包，但不会写出完整的解压文件，也不会把整份 dump 放入内存。单行实体在内存中解析；没有 P18 字段的行先快速跳过。JSON dump 的实体顺序不作假设。

导入每 2000 行提交一次进度，也可用 `import-dump --limit 10000` 在导入 10000 个匹配实体后暂停；重复相同命令会继续后面的记录。**压缩文件恢复仍需从开头解压到已提交的位置**，因此恢复大文件时可能先长时间没有新的导入计数；它不会重复插入数据。图像下载恢复不需要再扫描 dump。

一个输出目录绑定一个输入快照和范围。换 dump、移动或修改已绑定的 dump、改变 scope、把 sample 和全量混用，都会报错；使用新的输出目录即可。`state.sqlite3` 应位于本机磁盘，不建议放在网络共享文件系统上。

## 进度、重试与校验

```bash
# 可在采集进程运行时，从另一个终端查看。
python scripts/download_p18.py status --output data/p18-all

# 网络错误重试耗尽或文件不可用会保留 failed；只有加此参数才重试它们。
python scripts/download_p18.py download --output data/p18-all --retry-failed

# 完整 SHA-256 校验；重新排队丢失或损坏文件，然后重新下载。
python scripts/download_p18.py verify --output data/p18-all --repair
python scripts/download_p18.py download --output data/p18-all

# 数据更新后重新导出，导出文件不是实时同步的。
python scripts/download_p18.py export --output data/p18-all
```

正常恢复通过文件存在性和字节数快速检查；同长度损坏需要 `verify` 的哈希校验才能发现。连续失败达到默认 10 次时停止本轮，避免在网络阻断时持续请求。缩略图不可用时记录失败，不会偷偷改下大幅原图；`--width 0` 是显式请求原图，但原始 SVG 等 Pillow 无法解码的格式会失败，因此推荐默认缩略图。

如果旧版出现 `Unexpected Commons image host: thumb.wikimedia.org`，这是缩略图域名兼容问题。[Wikimedia 已将缩略图迁到该域名](https://wikitech.wikimedia.org/wiki/Thumbor)，当前脚本支持它和原图域名 `upload.wikimedia.org`。更新脚本后，在原输出目录执行 `download --retry-failed` 即可恢复；不要删除 SQLite 或重新采集清单。比如此前使用 500 像素的小样本：

```bash
python scripts/download_p18.py download --output data/p18-pilot --width 500 --retry-failed
python scripts/download_p18.py export --output data/p18-pilot
```

退出码：0 为当前命令成功；1 为本轮错误；2 表示仍有下载失败，或校验发现损坏；130 为键盘中断。使用 `--limit` 成功退出只表示本次限量任务完成，不表示全量完成。确认 `dump_progress.complete=true`，并检查文件状态中是否还有 pending/failed。

## 产物

```text
data/p18-all/
  state.sqlite3           # 断点和完整状态，核心数据，保留好
  images/ab/<file_id>.jpg # 按文件名哈希分片，实际扩展名来自图像解码
  entities.jsonl          # 一行一个条目：名字、别名、描述、全部 P18 声明等
  pairs.jsonl             # 一行一个 QID—图片对应，包含成功和失败状态
  summary.json            # 数量、字节数和完成情况
```

图片路径相对于输出目录。`file_id` 是规范化 Commons 文件名的 SHA-256；`sha256` 是实际下载图像内容的 SHA-256，两者用途不同。`source.sha1` 和 `source.size` 属于 Commons 原始文件，不能拿来校验缩略图。作者与许可等字段位于 `source.extmetadata`，可能含 HTML，展示时应作为数据处理。

使用图像时先筛选 `pairs.jsonl` 中 `status == "downloaded"`；同一图片可能对应多个 QID，身份标注检查、跨集合去重和训练/验证划分需要考虑这一点。Wikidata 的结构化数据许可与图片各自的许可分别保留。

## 开发验证

```bash
python -m unittest discover -s tests -v
python scripts/download_p18.py --help
```

离线测试覆盖非人物收集、多 P18/rank、跨实体图片复用、压缩 dump 恢复与截断、Commons 规范化/重定向、限流退避、坏图处理、断点恢复和哈希修复，不会启动全量下载。

实现参考：[官方 dump 格式](https://www.wikidata.org/wiki/Wikidata:Database_download)、[Commons imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo)、[API 请求约定](https://www.mediawiki.org/wiki/API:Etiquette)、[公开查询服务限制](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)。
