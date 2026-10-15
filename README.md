# Wikidata 人物名字—P18 图像采集

下载带有 P18 的 Wikidata 人物及其 Commons 图片，供后续人物—图片数据建设使用。`sample` 和 `import-dump` 默认只保留具有非弃用、直接 `P31=Q5` 声明的条目；全量入口还排除完全没有 Wikidata label、只能以 QID 充当姓名的条目。确实需要地点、作品等其他类型时必须显式传 `--scope all` 并使用单独输出目录。

全量入口默认使用 **500 像素宽的 Commons 缩略图**。原图较小时可能返回原图尺寸；纵向图片的高度可能大于 500。每个输出目录固定一个分辨率配置。单独调用 Python 的 `download` 子命令时，首次运行的默认宽度仍为 960，可用 `--width 500` 指定。

## 直接下载全量人物库

在已经安装依赖、设置好 `P18_USER_AGENT` 的 Google Cloud Linux 机器上，运行：

```bash
bash scripts/download_all_p18.sh
```

该命令会连续完成：下载官方完整 JSON dump → 校验 → 提取具有真实 label 的 Q5 人物及其全部有效 P18 → 下载对应图片 → 导出名字—图像对。**没有 200/1000 条上限，也没有中途的试跑步骤。** 默认输出到 `data/p18-humans`，中断后重复相同命令即可恢复。非人物实体不会进入清单，也不会发起 Commons 图片请求。

默认选用带日期的 `20261005` gzip 快照，解压速度通常比 bz2 快。可用 `P18_DUMP_DATE` 选择官方目录中其他已完成快照，用 `P18_DUMP_COMPRESSION=bz2` 选择体积更小的压缩包，用 `P18_IMAGE_WIDTH` 调整图片宽度。`P18_ARCHIVE_WORKERS` 控制 Internet Archive 补源连接数，默认 10，范围 0–10；设为 0 可关闭补源。已有输出目录继续使用原宽度。长任务可以直接放进 `tmux`：

```bash
tmux new -s wikidata-p18 'bash scripts/download_all_p18.sh'
```

查看进度：

```bash
python scripts/download_p18.py status --output data/p18-humans
```

`sample` 只创建小清单，删除它的 `--limit` 仍默认取 200 条；`download` 只处理所在目录已经有的清单，删除它的 `--limit` 不会把小清单扩大为全量。全量使用上面的入口。

## 工程方案与采集语义

1. `sample` 获取少量在线条目，验证网络和目录；`import-dump` 从官方完整 JSON dump 逐行提取全量清单。全量采集不依赖公开 SPARQL 服务的分页或大型查询。
2. 默认只收集直接具有非弃用 `P31=Q5` 的人物；全量脚本还要求至少一个真实 label。人物的全部非弃用 P18 都保留，包括 normal 和 preferred；忽略 deprecated、novalue、somevalue。保留声明 ID、rank、限定符和引用。此处“有效声明”只表示结构上有值且未弃用，不表示图片一定含有可识别真人脸。
3. 姓名优先中文、简体中文、英文，缺少时取其余可用 label。底层命令未启用 `--require-label` 时，完全没有 label 的条目会用 QID 并明确标记；全量人物入口会直接排除这些条目。保留所有语言的 labels、aliases、descriptions，及 P31 类型、P373 分类、Wikidata 版本号。
4. 通过 Commons `imageinfo` 批量获取缩略图地址、原图地址、来源页、尺寸、格式和原图 SHA-1 等必要信息。默认不请求开销较大的 `extmetadata`（作者、许可文本、长说明）；需要在新下载时获取这些字段可加 `--full-metadata`。一个实体多张 P18 全部保留；多个实体共享同一规范化文件名时，只下载一次文件。
5. SQLite 保存实体、对应关系、下载状态和 dump 进度；图片先在本机 `staging/` 写 `.part`、实际解码并计算 SHA-256，再交给独立线程写入最终图片目录。只有最终写入、同步和关闭成功，SQLite 才标记为 `downloaded`，随后清除暂存。对象存储上传失败或中断时保留本地副本供下次补传。
6. 下载阶段不做人脸检测或身份验证。所有导出记录的 `reference_verified` 都是 `false`；不得直接把下载完成当成已经确认的 GT。

Python 3.10+，Linux/macOS。CPU 机器即可。元数据 API 串行预取，默认每批最多 **50 个文件、提前缓存 200 个链接**；默认 2 条 Wikimedia 图片连接与 4 个存储写入线程。只查必要字段，链接队列独立于图片暂存预算；HTTP 连接空闲保留 60 秒，减少 API 等待后重新建连。图片完成后立即补入下一张，也不等待整批图片完成。所有图片读取（包括 Internet Archive）共享按滚动 10 秒窗口计算的 25 Mbps 字节预算。Wikimedia 原图按缓存命中、未命中的两类预算调度，缩略图按共享最小请求间隔错峰；收到 429 后，两条 Wikimedia 连接共同遵守 `Retry-After`。5xx 和 `maxlag` 也会退避。[Wikimedia 媒体下载规则](https://wikitech.wikimedia.org/wiki/Robot_policy#Media_API_rules)要求最多 2 条媒体连接及 25 Mbps 总带宽，增加存储写入线程不会增加 Wikimedia 图片连接数。

默认额外使用最多 10 条 Internet Archive 连接，为适用的旧原图提供补源。仅在任务本来就需要原图时尝试归档，取回内容必须与 Commons 元数据中的原始 SHA-1 和字节数一致，之后沿用相同的解码、暂存和写入流程。归档缺失、请求失败或校验不符时回到 Commons 下载；500px 缩略图仍按原来的宽度获取。补源不会改变清单、图片质量或已完成状态，实际提速取决于旧原图覆盖率和网络情况。

## 安装

以下命令在 Google Cloud 机器的仓库目录运行。数据目录应位于容量足够的持久磁盘；本机无需先下载数据再上传。建议用 `tmux` 保持会话。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 改成你的真实联系邮箱或公开项目/用户页；无需 API Key。
export P18_USER_AGENT='WikidataP18Bot/0.1 (mailto:YOUR_REAL_EMAIL)'

bash scripts/download_all_p18.sh
```

## 可选：小样本调试

只有需要调试时才使用 `sample`。例如指定几个条目：

```bash
python scripts/download_p18.py sample --output data/p18-qids --qids Q42 Q90 Q517
python scripts/download_p18.py download --output data/p18-qids
python scripts/download_p18.py export --output data/p18-qids
```

`sample` 默认范围是 `humans`，上限 1000。它是在线查询返回的前一批结果，不是随机样本，更不代表完整数据集。第一次取到的 ID 列表会保存，恢复时不重新选样。在线数据可能在查询与取条目之间变更；不再是直接 Q5、没有 P18 或启用 `--require-label` 后没有 label 的条目会记入日志。

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

校验通过后建立有姓名的人物清单，再下载图像：

```bash
python scripts/download_p18.py import-dump \
  --dump downloads/wikidata-20261005-all.json.bz2 \
  --output data/p18-humans \
  --scope humans \
  --require-label

python scripts/download_p18.py download --output data/p18-humans --width 500 --retry-failed
python scripts/download_p18.py export --output data/p18-humans
```

首轮导入读完整个压缩包，但不会写出完整的解压文件，也不会把整份 dump 放入内存。单行实体在内存中解析；没有 P18 字段的行先快速跳过。JSON dump 的实体顺序不作假设。

导入每 2000 行提交一次进度，也可用 `import-dump --limit 10000` 在导入 10000 个匹配实体后暂停；重复相同命令会继续后面的记录。**压缩文件恢复仍需从开头解压到已提交的位置**，因此恢复大文件时可能先长时间没有新的导入计数；它不会重复插入数据。图像下载恢复不需要再扫描 dump。

一个输出目录绑定一个输入快照和范围。换 dump、移动或修改已绑定的 dump、改变 scope、把 sample 和全量混用，都会报错；使用新的输出目录即可。`state.sqlite3` 应位于本机磁盘，不建议放在网络共享文件系统上。

## 从旧的 `scope: all` 建立干净人物库

已有完整 `data/p18-all/state.sqlite3` 时，不需要再次解压 156 GB dump。`derive` 直接从旧 SQLite 读取已经保存的 `is_human`、label 和 P18 关系，建立紧凑的新数据库。它只复制人物清单，所有图片状态都重置为 `pending`；不会复制旧的 Commons URL、下载状态、哈希或图片路径，也不会修改源数据库和旧图片目录。派生完成前新旧 SQLite 会同时存在，先用 `du -h data/p18-all/state.sqlite3` 和 `df -h data` 确认本机磁盘还有足够空间。

先停止旧下载器并只读查看精确规模：

```bash
cd /home/yingjia/face_name_data

if pgrep -af '[d]ownload_p18.py.*data/p18-all'; then
  echo '旧下载器仍在运行，请先按一次 Ctrl+C 等它完整退出。' >&2
  exit 1
fi

.venv/bin/python scripts/download_p18.py derive \
  --source data/p18-all \
  --output data/p18-humans \
  --scope humans \
  --require-label \
  --dry-run
```

输出会列出人物数、人物—图片关系数、唯一图片数、旧状态分布、其中已经下载的字节数，以及被排除的无 label 人物数。确认后创建新数据库：

```bash
.venv/bin/python scripts/download_p18.py derive \
  --source data/p18-all \
  --output data/p18-humans \
  --scope humans \
  --require-label
```

为干净人物库使用新的对象存储前缀，不能再次指向旧的混合图片目录：

```bash
(
set -e
mountpoint -q /mnt/algo_data

new_images=/mnt/algo_data/yingjia/wikidata-human-p18/20261005-w500/images
test ! -e data/p18-humans/images
mkdir -p -- "$new_images"
ln -s -- "$new_images" data/p18-humans/images
test "$(readlink -f data/p18-humans/images)" = "$new_images"

.venv/bin/python scripts/download_p18.py status --output data/p18-humans
)
```

`status` 应显示 `catalog_source.kind` 为 `derived`、`scope` 为 `humans`、`require_label` 为 `true`、`downloaded_bytes` 为 0，且全部文件都是 `pending`。这些条件成立后，旧混合图片和旧状态可以删除。下面的守卫要求软链接精确指向已知旧路径，并确认新旧路径不同；任一条件不符都会在 `rm` 前停止：

```bash
(
set -e
mountpoint -q /mnt/algo_data

if pgrep -af '[d]ownload_p18.py.*data/p18-(all|humans)'; then
  echo '仍有旧库或新库下载器在运行，先按一次 Ctrl+C 等它完整退出。' >&2
  exit 1
fi

old_images="$(readlink -f data/p18-all/images)"
new_images="$(readlink -f data/p18-humans/images)"
expected_old=/mnt/algo_data/yingjia/wikidata-p18/20261005-w500/images

test "$old_images" = "$expected_old"
test "$new_images" = /mnt/algo_data/yingjia/wikidata-human-p18/20261005-w500/images
test "$old_images" != "$new_images"

.venv/bin/python - <<'PY'
import json
import sqlite3
from pathlib import Path

state = Path('data/p18-humans/state.sqlite3').resolve()
with sqlite3.connect(state.as_uri() + '?mode=ro', uri=True) as db:
    row = db.execute("SELECT value FROM meta WHERE key='catalog_source'").fetchone()
    source = json.loads(row[0]) if row else {}
    statuses = dict(db.execute('SELECT status, COUNT(*) FROM files GROUP BY status'))
    downloaded_bytes = db.execute(
        "SELECT COALESCE(SUM(nbytes), 0) FROM files WHERE status='downloaded'"
    ).fetchone()[0]
    entity_count = db.execute('SELECT COUNT(*) FROM entities').fetchone()[0]

assert source.get('kind') == 'derived', source
assert source.get('scope') == 'humans', source
assert source.get('require_label') is True, source
assert source.get('downloads_reset') is True, source
assert entity_count > 0, entity_count
assert set(statuses) == {'pending'} and statuses['pending'] > 0, statuses
assert downloaded_bytes == 0, downloaded_bytes
print(f'新人物库删除保护检查通过：{entity_count} 人，{statuses["pending"]} 个待下载文件')
PY

rm -rf -- "$old_images"
rm -rf -- data/p18-all
)
```

官方 dump 和校验文件位于 `downloads/`，上述清理不会删除它们。新人物库从零下载：

```bash
.venv/bin/python scripts/download_p18.py download \
  --output data/p18-humans \
  --width 500 \
  --workers 2 \
  --upload-workers 8 \
  --staging-mib 1024 \
  --skip-existing-check \
  --user-agent "face-name-data-bot/0.1 (mailto:yk_chai@yeah.net)"
```

## 对象存储与连续下载

支持使用 `data/p18-humans/images` 指向对象存储挂载目录的软链接。SQLite、dump 和 `data/p18-humans/staging` 留在本机；`staging` 不能另行软链接到对象存储。更新脚本后，等旧下载进程退出，在原输出目录按原宽度启动即可接着下载，保留现有图片软链接、SQLite 和暂存。启用补源无需 `derive`、重建数据库或重新导入 dump；已有派生人物库直接运行 `download`，不使用面向 dump 导入的全量脚本。

对于已经完成图片迁移、验证过已有图片，并将对象存储挂载在 `/mnt/algo_data` 的 Google Cloud Linux 机器：

```bash
(
set -e
cd /home/yingjia/face_name_data
mountpoint -q /mnt/algo_data

.venv/bin/python scripts/download_p18.py download \
  --output data/p18-humans \
  --width 500 \
  --workers 2 \
  --archive-workers 10 \
  --upload-workers 8 \
  --staging-mib 1024 \
  --skip-existing-check \
  --retry-failed
)
```

该命令沿用已设置的 `P18_USER_AGENT`；也可显式传 `--user-agent`。默认处理整个清单，没有数量上限。需要关闭 Internet Archive 补源时，把 `--archive-workers 10` 改为 `--archive-workers 0`，其余参数和数据目录保持不变。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--workers` | `2` | Wikimedia 图片下载连接数，只能为 1 或 2。 |
| `--archive-workers` | `10` | Internet Archive 补源连接数，范围 0–10；0 关闭。归档图片必须通过原始 SHA-1 和字节数校验，否则回退到 Commons。 |
| `--upload-workers` | `4` | 最终图片目录的并发写入数；当前对象存储实测无积压，示例使用 8。只有 `storing` 持续增长时才需要提高。 |
| `--metadata-batch-size` | `50` | 每次解析的文件数量，上限 50；过长的文件名列表自动改用 POST，避免 URL 长度限制。 |
| `--metadata-prefetch` | `200` | 提前准备的图片链接数量；链接只占少量内存，不占图片暂存额度。 |
| `--full-metadata` | 关闭 | 解析新链接时同时取作者、许可文本和长说明；开启后每批最多 5 张。不会为已完成图片重新下载或自动补齐其元数据。 |
| `--bandwidth-mbps` | `25` | 所有图片来源共享的总带宽上限，单位为兆比特/秒，上限 25；按滚动 10 秒窗口统计并包含重试数据。 |
| `--staging-mib` | `1024` | 本地暂存图片的预算，必须至少容纳一张 `--max-image-mib` 规定的最大图片。 |
| `--skip-existing-check` | 关闭 | 直接相信 SQLite 中的 `downloaded` 状态，跳过对象存储上的逐文件存在性/大小查询。缺失或损坏的旧文件需用 `verify --repair` 发现并重新排队。 |
| `--log-interval` | `15` | 每隔多少秒输出一条速度、剩余量和耗时统计。 |
| `--verbose` | 关闭 | 打开逐条 HTTP 请求日志；默认仅输出进度、重试与错误。 |

上传队列随 `--upload-workers` 扩展，超过 32 个上传线程也能实际运行，不受固定 32 个任务的限制。链接队列默认最多 200 个；每次开始下载前按单张图片的最大允许大小预留空间，下载完成后改按实际字节占用暂存额度。这样许多几十 KB 的待上传小图不会各占一个 20 MiB 的预算槽位。写入变慢时仍会受到任务队列和暂存预算的约束，避免持续堆积。暂存预算计算包括之前失败而保留的图片，不包含少量 JSON 元数据及挂载工具自身的缓存；目录空闲空间不足仍会报错停止。若已有失败暂存占满预算，使用 `--retry-failed` 优先补传它们，或显式提高预算。

上面的对象存储命令使用 8 个上传线程。此前云端日志中 `store` 约 0.08 秒且只有 0–1 个存储任务，64 个线程没有参与实际加速。如果以后 `storing` 持续积压，可以逐步提高；存储没有积压时，提高它不会加快源站取图。图片最终仍通过 `images` 软链接写入对象存储，SQLite 和暂存留在本机。

图片下载与校验只操作本地暂存，最终图片通过挂载目录直接写入最终文件名，避免远端 `.part`、校验回读和改名产生的额外操作。临时存储 I/O 错误会重试两次；写满、无权限、只读则停止。只有 SQLite 提交为 `downloaded` 后，文件才视为完成；中断可能在对象目录留下尚未完成的文件，重跑会覆盖它们。因此下游应读取 SQLite 或导出结果的成功记录，而不是直接扫描图片目录。

每条进度包含 `files/s`（最近约一分钟的处理速度，含失败）、`MiB/s stored`（成功写入的字节速度）、本轮剩余任务数、`ETA`、`avg fetch`（下载和本地校验平均耗时）、`bandwidth wait`（每张图因滚动带宽窗口主动等待的平均秒数）、`media gap`（两次图片请求开始之间的固定最小错峰时间）、`429 响应数/共享冷却次数`、共享冷却累计秒数、`store`（最终存储平均耗时）、`metadata`（每批链接查询的平均耗时和文件数）、`link wait`（有下载空位与暂存空间时等待链接的累计秒数）、`fetching`、`storing`（上传中和排队中的任务数）、`ready`（已准备好的链接数）、复用暂存数及暂存额度。`ready` 长期为 0 且 `link wait` 增长说明链接供应不足；待上传任务持续增加则说明最终存储跟不上。ETA 估计的是本轮结束时间，失败文件仍需重试；不能把离线测试速度当成云端实测速度。

`Retry image ... HTTP 429; shared cooldown` 表示 Wikimedia 图片 CDN 要求暂缓，本轮还没有产生失败。两条 Wikimedia 图片线程会共同遵守 `Retry-After`，随后继续按原图、缩略图各自的请求预算调度。日志中的 `429 responses ... across ... cooldowns` 可区分同一冷却窗口内的两个在途响应和独立限流事件。`Retry api` 来自 Commons/Wikidata 元数据接口，单独冷却。

`source.metadata_profile=basic` 表示这次只查询必要字段，作者和许可文本没有在本次请求中获取。原有详细缓存会继续复用；失败项重新解析为轻量信息时，早先的详细信息及其获取时间保存在 `source.previous_extmetadata`。需要这些字段时可沿来源页另行获取。

## 进度、重试与校验

```bash
# 可在采集进程运行时，从另一个终端查看。
python scripts/download_p18.py status --output data/p18-humans

# 网络错误重试耗尽或文件不可用会保留 failed；只有加此参数才重试它们。
python scripts/download_p18.py download --output data/p18-humans --retry-failed

# 完整 SHA-256 校验；重新排队丢失或损坏文件，然后重新下载。
python scripts/download_p18.py verify --output data/p18-humans --repair
python scripts/download_p18.py download --output data/p18-humans

# 数据更新后重新导出，导出文件不是实时同步的。
python scripts/download_p18.py export --output data/p18-humans
```

默认恢复通过文件存在性和字节数检查；`--skip-existing-check` 可跳过这一步。同长度损坏需要 `verify` 的哈希校验才能发现。上传失败的本地暂存会在重试时校验哈希并补传，内容损坏则重新下载。单个 Commons 文件缺失、HTTP 404/410、坏图、格式或大小不适用会记为失败并继续，不计入系统故障阈值；连续网络或存储故障达到默认 10 次时停止接收新任务。按一次 Ctrl+C 也会停止新任务，等待已经开始的存储写入退出并保留暂存，之后重复命令恢复。缩略图不可用时记录失败，不会偷偷改下大幅原图；`--width 0` 是显式请求原图，但原始 SVG 等 Pillow 无法解码的格式会失败，因此推荐默认缩略图。

如果旧版出现 `Unexpected Commons image host: thumb.wikimedia.org`，这是缩略图域名兼容问题。[Wikimedia 已将缩略图迁到该域名](https://wikitech.wikimedia.org/wiki/Thumbor)，当前脚本支持它和原图域名 `upload.wikimedia.org`。更新脚本后，在原输出目录执行 `download --retry-failed` 即可恢复；不要删除 SQLite 或重新采集清单。比如此前使用 500 像素的小样本：

```bash
python scripts/download_p18.py download --output data/p18-pilot --width 500 --retry-failed
python scripts/download_p18.py export --output data/p18-pilot
```

退出码：0 为当前命令成功；1 为本轮错误；2 表示仍有下载失败，或校验发现损坏；130 为键盘中断。使用 `--limit` 成功退出只表示本次限量任务完成，不表示全量完成。确认 `dump_progress.complete=true`，并检查文件状态中是否还有 pending/failed。

## 产物

```text
data/p18-humans/
  state.sqlite3           # 断点和完整状态，核心数据，保留好
  staging/                # 本地待上传图片和校验记录；成功写入、提交后自动清除
  images/ab/<file_id>.jpg # 按文件名哈希分片，实际扩展名来自图像解码
  entities.jsonl          # 一行一个条目：名字、别名、描述、全部 P18 声明等
  pairs.jsonl             # 一行一个 QID—图片对应，包含成功和失败状态
  summary.json            # 数量、字节数和完成情况
```

图片路径相对于输出目录。`file_id` 是规范化 Commons 文件名的 SHA-256；`sha256` 是实际下载图像内容的 SHA-256，两者用途不同。`source.sha1` 和 `source.size` 属于 Commons 原始文件，不能拿来校验缩略图。请求了详细元数据时，作者与许可等字段位于 `source.extmetadata`，可能含 HTML，展示时应作为数据处理。

使用图像时先筛选 `pairs.jsonl` 中 `status == "downloaded"`；同一图片可能对应多个 QID，身份标注检查、跨集合去重和训练/验证划分需要考虑这一点。Wikidata 的结构化数据许可与图片各自的许可分别保留。

## 开发验证

```bash
python -m unittest discover -s tests -v
python scripts/download_p18.py --help
```

离线测试覆盖非人物收集、多 P18/rank、跨实体图片复用、压缩 dump 恢复与截断、Commons 规范化/重定向、限流退避、坏图与 MPO 处理、断点恢复和哈希修复；也覆盖 50 张轻量批次、200 个链接预取、超过 32 个并发上传、全局字节限速和实际暂存预算、上传失败及中断后的无网络补传、暂存损坏重下载、对象目录软链接、快速续传和单文件错误不停止全量任务。不会启动全量下载。

实现参考：[官方 dump 格式](https://www.wikidata.org/wiki/Wikidata:Database_download)、[Commons imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo)、[API 请求约定](https://www.mediawiki.org/wiki/API:Etiquette)、[公开查询服务限制](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)。

## 2026-10-14 本机连接实测

使用本机现有 curl 与 HTTPX，保留 TLS 校验，串行调用公开 API；图片最多两路、合计限速低于 25 Mbps。元数据对照来自同一份 50 个 Commons 文件名单，先用其中两个 5 张批次比较字段开销，再以整份名单测试 50 张轻量批次。这是少量样本，受到网络波动、测试顺序和 CDN 缓存影响。

| 测试 | 本机结果 | 对下载器的影响 |
| --- | --- | --- |
| 每批 5 张，附带 `extmetadata` | 两次 HTTP 耗时 1.34 / 1.97 秒，响应约 21 / 22 KB | 默认去掉这个昂贵字段 |
| 每批 5 张，只取必要字段 | 1.01 / 1.03 秒，响应约 8.8 / 9.9 KB | 单张返回数据减少 |
| 每批 50 张，只取必要字段 | 1.52 / 1.51 秒，响应约 91 KB | 每次最多获取十倍数量的链接 |
| HTTPX 空闲保留 5 秒，间隔 5.2 秒再请求 | 第二次没有复用连接，耗时 1.47 秒 | API 等待后可能反复 TLS 建连 |
| HTTPX 空闲保留 60 秒，同样等待后再请求 | 第二次复用了连接，耗时 0.45 秒 | 连接空闲保留时间改为 60 秒 |
| HTTP/1.1 与 HTTP/2 的图片连接复用 | 两种都能复用；本轮未见 HTTP/2 稳定胜出 | 保留现有依赖和 HTTP/1.1，通过持久连接提速 |
| 新版实际下载 50 张缩略图 | 42.24 秒，50 成功、0 失败，50 张哈希校验通过；离线续跑处理 0 张 | 真实取图、校验和恢复已验证；不是云端吞吐预测 |

本机对 `www.wikidata.org` 的两次 IPv4 请求（HTTP/1.1、HTTP/2）都在 TLS 阶段超时；Commons 元数据与图片请求成功。已导入完整清单的续传只读取本地 SQLite 并请求 Commons，不需要重新连接 Wikidata 或扫描 dump。本机无法测量云端 `/mnt/algo_data` 的性能；云端是否还有存储瓶颈应看上传任务积压、`store` 和 `link wait`。
