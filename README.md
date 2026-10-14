# Wikidata 名字—P18 图像采集

下载带有 P18 的 Wikidata 条目及其 Commons 图片，供后续数据建设使用。默认覆盖所有实体类别；人物、地点、作品等都会保留。`--scope humans` 可限定为具有非弃用 `P31=Q5` 声明的条目。

全量入口默认使用 **500 像素宽的 Commons 缩略图**。原图较小时可能返回原图尺寸；纵向图片的高度可能大于 500。每个输出目录固定一个分辨率配置。单独调用 Python 的 `download` 子命令时，首次运行的默认宽度仍为 960，可用 `--width 500` 指定。

## 直接下载全量

在已经安装依赖、设置好 `P18_USER_AGENT` 的 Google Cloud Linux 机器上，运行：

```bash
bash scripts/download_all_p18.sh
```

该命令会连续完成：下载官方完整 JSON dump → 校验 → 提取全部带 P18 的条目 → 下载全部对应图片 → 导出名字—图像对。**没有 200/1000 条上限，也没有中途的试跑步骤。** 默认输出到 `data/p18-all`，中断后重复相同命令即可恢复。已有的 `data/p18-pilot` 不参与这条全量任务。

默认选用带日期的 `20261005` gzip 快照，解压速度通常比 bz2 快。可用 `P18_DUMP_DATE` 选择官方目录中其他已完成快照，用 `P18_DUMP_COMPRESSION=bz2` 选择体积更小的压缩包，用 `P18_IMAGE_WIDTH` 调整图片宽度。长任务可以直接放进 `tmux`：

```bash
tmux new -s wikidata-p18 'bash scripts/download_all_p18.sh'
```

查看进度：

```bash
python scripts/download_p18.py status --output data/p18-all
```

`sample` 只创建小清单，删除它的 `--limit` 仍默认取 200 条；`download` 只处理所在目录已经有的清单，删除它的 `--limit` 不会把小清单扩大为全量。全量使用上面的入口。

## 工程方案与采集语义

1. `sample` 获取少量在线条目，验证网络和目录；`import-dump` 从官方完整 JSON dump 逐行提取全量清单。全量采集不依赖公开 SPARQL 服务的分页或大型查询。
2. 默认收集 Q/P 条目的全部非弃用 P18 声明，包括 normal 和 preferred；忽略 deprecated、novalue、somevalue。保留声明 ID、rank、限定符和引用。此处“有效声明”只表示结构上有值且未弃用，不表示图像正确。
3. 姓名优先中文、简体中文、英文，缺少时取其余可用 label；完全没有 label 时用 QID 并明确标记。保留所有语言的 labels、aliases、descriptions，及 P31 类型、P373 分类、Wikidata 版本号。
4. 通过 Commons `imageinfo` 获取缩略图地址、原图地址、原始尺寸、作者和许可等源信息。一个实体多张 P18 全部保留；多个实体共享同一规范化文件名时，只下载一次文件。
5. SQLite 保存实体、对应关系、下载状态和 dump 进度；图片先在本机 `staging/` 写 `.part`、实际解码并计算 SHA-256，再交给独立线程写入最终图片目录。只有最终写入、同步和关闭成功，SQLite 才标记为 `downloaded`，随后清除暂存。对象存储上传失败或中断时保留本地副本供下次补传。
6. 下载阶段不做人脸检测或身份验证。所有导出记录的 `reference_verified` 都是 `false`；不得直接把下载完成当成已经确认的 GT。

Python 3.10+，Linux/macOS。CPU 机器即可。元数据 API 串行预取，每批最多 5 个文件；默认 2 条图片连接与 4 个存储写入线程。图片完成后立即补入下一张，不再有固定的 0.5 秒请求间隔，也不再等待整批图片完成。图片连接共享 25 Mbps 字节限速；429、5xx、`maxlag` 仍会退避并遵守 `Retry-After`。[Wikimedia 媒体下载规则](https://wikitech.wikimedia.org/wiki/Robot_policy#Media_API_rules)要求最多 2 条媒体连接及 25 Mbps 总带宽，增加存储写入线程不会增加 Wikimedia 图片连接数。

## 安装

以下命令在 Google Cloud 机器的仓库目录运行。数据目录应位于容量足够的持久磁盘；本机无需先下载数据再上传。建议用 `tmux` 保持会话。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 改成你的真实联系邮箱或公开项目/用户页；无需 API Key。
export P18_USER_AGENT='WikidataP18Downloader/0.1 (mailto:YOUR_REAL_EMAIL)'

bash scripts/download_all_p18.sh
```

## 可选：小样本调试

只有需要调试时才使用 `sample`。例如指定几个条目：

```bash
python scripts/download_p18.py sample --output data/p18-qids --qids Q42 Q90 Q517
python scripts/download_p18.py download --output data/p18-qids
python scripts/download_p18.py export --output data/p18-qids
```

`sample` 默认范围也是 `all`，上限 1000。它是在线查询返回的前一批结果，不是随机样本，更不代表完整数据集。第一次取到的 ID 列表会保存，恢复时不重新选样。在线数据可能在查询与取条目之间变更；不再具有 P18 的条目会记入日志。

## 全量：在云端直接下载官方 dump

以下是逐阶段执行方式；上面的一条命令入口已经自动串联这些步骤。从[官方目录](https://dumps.wikimedia.org/wikidatawiki/entities/)选择**带日期的完整 JSON dump**，避免用持续变化的 `latest` 链接跨日续传。下面是 2026-10-12 核验过的 2026-10-05 快照示例，后续旧快照可能被轮换清理。`.bz2` 压缩包约 103 GB，`.gz` 约 156 GB；gzip 通常解压更快，bz2 更节省暂存空间。图片、SQLite 和导出文件另外占用磁盘空间。

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

python scripts/download_p18.py download --output data/p18-all --width 500 --retry-failed
python scripts/download_p18.py export --output data/p18-all
```

首轮导入读完整个压缩包，但不会写出完整的解压文件，也不会把整份 dump 放入内存。单行实体在内存中解析；没有 P18 字段的行先快速跳过。JSON dump 的实体顺序不作假设。

导入每 2000 行提交一次进度，也可用 `import-dump --limit 10000` 在导入 10000 个匹配实体后暂停；重复相同命令会继续后面的记录。**压缩文件恢复仍需从开头解压到已提交的位置**，因此恢复大文件时可能先长时间没有新的导入计数；它不会重复插入数据。图像下载恢复不需要再扫描 dump。

一个输出目录绑定一个输入快照和范围。换 dump、移动或修改已绑定的 dump、改变 scope、把 sample 和全量混用，都会报错；使用新的输出目录即可。`state.sqlite3` 应位于本机磁盘，不建议放在网络共享文件系统上。

## 对象存储与连续下载

支持继续使用 `data/p18-all/images` 指向对象存储挂载目录的软链接。SQLite、dump 和 `data/p18-all/staging` 留在本机；`staging` 不能另行软链接到对象存储。更新脚本后，等旧下载进程退出，在原输出目录启动即可接着下载，无需重新导入 dump。

对于已经完成图片迁移、验证过已有图片，并将对象存储挂载在 `/mnt/algo_data` 的 Google Cloud Linux 机器：

```bash
(
set -e
mountpoint -q /mnt/algo_data

python scripts/download_p18.py download \
  --output data/p18-all \
  --width 500 \
  --workers 2 \
  --upload-workers 8 \
  --staging-mib 1024 \
  --skip-existing-check \
  --retry-failed
)
```

该命令在仓库目录运行，沿用已设置的 `P18_USER_AGENT`；也可显式传 `--user-agent`。默认处理整个清单，没有数量上限。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--workers` | `2` | Wikimedia 图片下载连接数，只能为 1 或 2。 |
| `--upload-workers` | `4` | 最终图片目录的并发写入数；挂载对象存储时可设为 8。 |
| `--bandwidth-mbps` | `25` | 两条图片连接共享的总带宽上限，单位为兆比特/秒，上限 25；包含重试数据。 |
| `--staging-mib` | `1024` | 本地暂存图片的预算，必须至少容纳一张 `--max-image-mib` 规定的最大图片。 |
| `--skip-existing-check` | 关闭 | 直接相信 SQLite 中的 `downloaded` 状态，跳过对象存储上的逐文件存在性/大小查询。缺失或损坏的旧文件需用 `verify --repair` 发现并重新排队。 |
| `--log-interval` | `15` | 每隔多少秒输出一条速度、剩余量和耗时统计。 |
| `--verbose` | 关闭 | 打开逐条 HTTP 请求日志；默认仅输出进度、重试与错误。 |

内存中的任务队列最多容纳 32 个文件；每次下载前按单张图片的最大允许大小预留暂存额度。写入变慢时队列自动等待，避免持续堆积。暂存预算计算包括之前失败而保留的图片，不包含少量 JSON 元数据及挂载工具自身的缓存；目录空闲空间不足仍会报错停止。若已有失败暂存占满预算，使用 `--retry-failed` 优先补传它们，或显式提高预算。

图片下载与校验只操作本地暂存，最终图片通过挂载目录直接写入最终文件名，避免远端 `.part`、校验回读和改名产生的额外操作。临时存储 I/O 错误会重试两次；写满、无权限、只读则停止。只有 SQLite 提交为 `downloaded` 后，文件才视为完成；中断可能在对象目录留下尚未完成的文件，重跑会覆盖它们。因此下游应读取 SQLite 或导出结果的成功记录，而不是直接扫描图片目录。

每条进度包含 `files/s`（最近约一分钟的处理速度，含失败）、`MiB/s stored`（成功写入的字节速度）、本轮剩余任务数、`ETA`、`avg fetch`（下载和本地校验平均耗时）、`store`（最终存储平均耗时）、复用暂存数及暂存预留额度。ETA 估计的是本轮结束时间，失败文件仍需重试；不能把离线测试速度当成云端实测速度。

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

默认恢复通过文件存在性和字节数检查；`--skip-existing-check` 可跳过这一步。同长度损坏需要 `verify` 的哈希校验才能发现。上传失败的本地暂存会在重试时校验哈希并补传，内容损坏则重新下载。连续失败达到默认 10 次时停止接收新任务；按一次 Ctrl+C 也会停止新任务，等待已经开始的存储写入退出并保留暂存，之后重复命令恢复。缩略图不可用时记录失败，不会偷偷改下大幅原图；`--width 0` 是显式请求原图，但原始 SVG 等 Pillow 无法解码的格式会失败，因此推荐默认缩略图。

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
  staging/                # 本地待上传图片和校验记录；成功写入、提交后自动清除
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

离线测试覆盖非人物收集、多 P18/rank、跨实体图片复用、压缩 dump 恢复与截断、Commons 规范化/重定向、限流退避、坏图与 MPO 处理、断点恢复和哈希修复；也覆盖跨批次连续下载、存储阻塞与下载分离、全局字节限速、暂存预算、上传失败及中断后的无网络补传、暂存损坏重下载、对象目录软链接和快速续传。不会启动全量下载。

实现参考：[官方 dump 格式](https://www.wikidata.org/wiki/Wikidata:Database_download)、[Commons imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo)、[API 请求约定](https://www.mediawiki.org/wiki/API:Etiquette)、[公开查询服务限制](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)。
