# MinerU 跨页表格图片匹配修复原理

## 1. 问题背景

MinerU 生成 `*_content_list_v2.json` 时，跨页表格的 HTML 内容可能已经合并，但表格图片仍然按页面分别生成在 `images` 目录中。

典型情况如下：

```json
{
    "type": "table",
    "content": {
        "image_source": {
            "path": "images/first-page.jpg"
        },
        "html": "..."
    }
}
```

后续页面的表格节点可能只有：

```json
{
    "type": "table",
    "content": {
        "image_source": {
            "path": "images/"
        }
    }
}
```

这会导致 JSON 中无法完整表达跨页表格对应的所有图片。

修复后的结构是在首个表格节点中增加 `image_sources`：

```json
{
    "type": "table",
    "content": {
        "image_source": {
            "path": "images/first-page.jpg"
        },
        "image_sources": [
            {
                "path": "images/first-page.jpg"
            },
            {
                "path": "images/continuation-page.jpg"
            }
        ],
        "html": "..."
    }
}
```

`image_source` 保留首张图片，`image_sources` 保存完整图片集合，首图位于第一个元素。

## 2. 总体流程

解析完成后，`magic_pdf_parse_main` 和 `magic_pdf_parse_main2` 都会执行以下流程：

```text
aio_do_parse
    ↓
读取 *_content_list_v2.json
    ↓
读取 images 目录中的图片
    ↓
收集 JSON 和 HTML 中已经引用的图片
    ↓
计算未被引用的图片
    ↓
寻找缺失图片位置
    ↓
匹配图片与跨页表格
    ↓
向首个表格节点写入 image_sources
    ↓
生成新的 *_content_list_v2_1.json
```

如果所有图片都已经被 JSON 或 HTML 引用，则不修改文件。

当一个表格的 `image_sources` 包含两张或以上图片时，还会执行图片合并：

1. 按 `image_sources` 的顺序读取原始图片；
2. 按垂直方向拼接成一张新的 JPEG 图片；
3. 将新的图片保存到 V2.1 JSON 所在目录，不放入原始 `images` 目录；
4. 将 `content.image_source.path` 替换为合并图路径；
5. 保留 `image_sources` 原数组不变，其中第一个元素仍然是首图原始路径。

因此最终结果同时保留了原始分页图片明细和一张可直接展示的完整表格图片。

## 3. 图片引用收集

图片引用不只存在于 `content.image_source.path` 中，也可能存在于表格 HTML 的 `<img>` 标签中。因此匹配前会递归检查每个内容块：

1. 检查结构化字段中的 `path`。
2. 检查 `image_source`。
3. 检查 `image_sources`。
4. 检查 HTML 中的 `src="images/xxx.jpg"`。
5. 检查 HTML 中可能出现的 `url(images/xxx.jpg)`。

只有没有出现在这些引用中的图片，才会被认为是候选缺失图片。

这样可以避免把 HTML 中已经使用的图片误判为缺失图片。

## 4. 缺失位置识别

候选位置主要是以下内容块：

- `type` 为 `table`、`image` 或 `figure`；
- `image_source.path` 为空；
- 或者路径只有 `images/` 目录前缀。

对于跨页表格，后续页面通常表现为一个没有实际图片文件名的 `table` 节点，因此会被识别为缺失位置。

## 5. 图片匹配依据

匹配不是简单按文件名排序，而是综合使用以下信息：

### 5.1 页面块的宽高比

从 JSON 块的 `bbox` 计算表格区域宽高比：

```text
区域宽高比 = (x2 - x1) / (y2 - y1)
```

再与候选图片的宽高比比较。两者越接近，匹配分数越好。

### 5.2 图片面积

如果能够读取图片尺寸，则比较表格区域面积和图片面积的对数距离。该因素只作为辅助，因为图片裁剪范围和 JSON 的 `bbox` 不一定完全一致。

### 5.3 与首个表格图片的宽度一致性

跨页表格的各页通常来自同一个表格截图或同一渲染比例，因此图片宽度通常比较接近。

对于一个缺失的表格续页，会找到它之前最近的、已有有效 `image_source` 的表格节点，并将该图片作为锚点。候选图片与锚点图片的宽度差异会被重点计入匹配分数。

这一规则可以区分不同表格的图片。例如：

- 第一个跨页表格的主图宽度约为 1482，续页图宽度约为 1479；
- 另一个跨页表格的主图宽度约为 1279，续页图宽度约为 1282。

### 5.4 文档中的位置顺序

匹配位置按照 JSON 页面和块的顺序处理。多个缺失续页会依次分配给对应的缺失表格位置，并按照页面顺序加入 `image_sources`。

文件系统中的图片文件名是哈希值，不能依赖文件名字典序作为文档顺序，因此文件名排序只作为输入顺序，不能作为主要匹配依据。

## 6. 跨页表格归并

当缺失位置是一个 `table`，程序会向前查找最近的有效表格节点：

1. 当前节点是跨页续表节点；
2. 向前查找最近的 `table` 节点；
3. 该节点必须存在有效的 `image_source.path`；
4. 先将首图放入 `content.image_sources[0]`，再按页面顺序追加匹配到的续页图片。
5. 如果图片数量大于 1，则将这些原始图片纵向合并，并把合并图写入 `image_source`。

续页节点本身的空 `image_source` 不会被强行填充，因为它只是跨页表格的占位节点。这样可以保留 MinerU 原有页面结构，同时在首节点集中表达完整的图片集合。

如果缺失位置没有找到前置表格，则按普通图片缺失处理，直接补写该节点的 `image_source.path`。

## 7. 去重和幂等性

写入 `image_sources` 前会检查同一路径是否已经存在：

- 已存在的图片不会重复追加；
- 已经修复过的 JSON 再次处理不会重复增加内容；
- 图片和 HTML 引用已经一致时不会修改文件。

因此该修复逻辑可以安全地在每次解析完成后执行。

## 8. API 接入位置

`magic_pdf_parse_main` 增加了 `content_list_version` 配置，取值如下：

| 配置值 | 读取文件 |
| --- | --- |
| `default` | `_content_list.json` |
| `v2` | `_content_list_v2.json` |
| `v2_1` | `_content_list_v2_1.json` |

默认值为 `default`。V2 和 V2.1 的分页数组结构会保持原样返回，不再强行按旧版 `List[Dict]` 结构校验。

修复逻辑位于 `services/pdf_service.py` 的 `repair_content_list_v2` 方法。该方法读取 V2，生成 V2.1，不覆盖原始文件。

### `magic_pdf_parse_main`

`aio_do_parse` 完成后，在读取 Markdown、middle JSON、模型输出和图片数据之前读取 `*_content_list_v2.json` 并生成 `*_content_list_v2_1.json`。API 仍返回原有格式的 `_content_list.json`，原始 V2 文件保持不变。

### `magic_pdf_parse_main2`

`aio_do_parse` 完成后，在输出目录整理和移动之前执行同样的修复，确保最终输出目录中同时保留原始 V2 和新增的 V2.1 JSON。

## 9. 样本验证结果

对 `能力测试样本2.1-vlm` 验证时，识别出 3 张未被 JSON 或 HTML 引用的跨页表格图片，并匹配为：

```text
第一个跨页表格：5b179e676f05d...jpg
第二个跨页表格：42bee2c4c441...jpg
第二个跨页表格：a791b1cbb601...jpg
```

最终顺序为：

```json
"image_sources": [
    {"path": "images/19428d14f74ab2921d054c9540984d06d2dc3687aa853d4b7645aa2cdad7ccae.jpg"},
    {"path": "images/42bee2c4c441...jpg"},
    {"path": "images/a791b1cbb601...jpg"}
]
```

同时，HTML 中已有引用的图片没有被重复添加，首图会保留为 `image_sources[0]`。

## 10. 设计边界

该方法依赖 MinerU 输出中的以下稳定特征：

- 图片统一位于 `images` 目录；
- 图片路径使用 `images/<filename>` 形式；
- 跨页表格后续页面仍然输出 `table` 节点；
- 表格块通常包含 `bbox`；
- 同一跨页表格的图片宽度通常相近。

对于图片宽度、表格边界和页面结构都完全异常的文件，程序仍会记录候选匹配信息，但匹配结果可能需要人工复核。
