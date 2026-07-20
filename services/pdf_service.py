import json
import hashlib
import os
import sys
from typing import Dict, List
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from domain.dto.base_dto import BaseResultModel

from pathlib import Path

import shutil

from typing import List

from fastapi import UploadFile

from fastapi.responses import JSONResponse

import os

import json

import io

import uuid

from typing import List, Optional

from glob import glob

from base64 import b64encode


from configs.base_config import MAGIC_PDF_IMG_URL

from domain.dto.base_dto import BaseResultModel

from domain.dto.output.magic_pdf_parse_main_output import ImageData, MagicPdfParseMainOutput



from mineru.utils.enum_class import MakeMode

from mineru.cli.common import aio_do_parse, read_fn, pdf_suffixes, image_suffixes,office_suffixes

from mineru.cli.backend_options import BACKEND_VLM_ENGINE, DEFAULT_BACKEND, normalize_backend



from loguru import logger

from mineru.utils.guess_suffix_or_lang import guess_suffix_by_path
from services.match_mineru_missing_images import match_missing_images


def _middle_image_metadata(parse_dir: str, pdf_name: str) -> Dict[str, List[dict]]:
    """Index MinerU crop provenance by filename.

    The middle JSON is the only reliable link between hashed crop names and
    document pages.  The same crop can occur more than once in the structure,
    so values are kept as a list and deduplicated by page/type/bbox.
    """
    middle_path = os.path.join(parse_dir, f"{pdf_name}_middle.json")
    if not os.path.isfile(middle_path):
        return {}
    try:
        with open(middle_path, "r", encoding="utf-8") as fp:
            pages = json.load(fp).get("pdf_info", [])
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning(f"Cannot read image provenance from {middle_path}: {exc}")
        return {}

    result: Dict[str, List[dict]] = {}
    for page_index, page in enumerate(pages, start=1):
        stack = [page]
        seen = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                filename = value.get("image_path")
                bbox = value.get("bbox")
                block_type = value.get("type")
                if isinstance(filename, str):
                    key = (page_index, block_type, tuple(bbox) if isinstance(bbox, list) else None)
                    if key not in seen:
                        seen.add(key)
                        result.setdefault(filename, []).append({
                            "page_index": page_index,
                            "block_type": block_type,
                            "bbox": bbox,
                        })
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return result


def merge_table_source_images(parsed: object, parse_dir: str) -> bool:
    """Merge each multi-page table's source images into its primary image."""
    try:
        from PIL import Image
    except Exception:
        logger.warning("Pillow is unavailable; skip merging table source images")
        return False

    changed = False
    if not isinstance(parsed, list):
        return False

    for page in parsed:
        if not isinstance(page, list):
            continue
        for block in page:
            if not isinstance(block, dict) or block.get("type") != "table":
                continue
            content = block.get("content")
            sources = content.get("image_sources") if isinstance(content, dict) else None
            if not isinstance(sources, list) or len(sources) < 2:
                continue

            source_paths = [
                item.get("path") for item in sources
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
            if len(source_paths) != len(sources) or any(
                not path.startswith("images/") for path in source_paths
            ):
                continue

            image_paths = [os.path.join(parse_dir, path.replace("/", os.sep)) for path in source_paths]
            if not all(os.path.isfile(path) for path in image_paths):
                continue

            digest = hashlib.sha1("|".join(source_paths).encode("utf-8")).hexdigest()[:20]
            # V2.1 is self-contained at the same directory level as the JSON.
            merged_relative_path = f"{digest}_table_merged.jpg"
            merged_path = os.path.join(parse_dir, merged_relative_path)
            if not os.path.isfile(merged_path):
                try:
                    images = [Image.open(path).convert("RGB") for path in image_paths]
                    try:
                        width = max(image.width for image in images)
                        height = sum(image.height for image in images)
                        merged = Image.new("RGB", (width, height), "white")
                        y = 0
                        for image in images:
                            merged.paste(image, (0, y))
                            y += image.height
                        merged.save(merged_path, format="JPEG", quality=95)
                    finally:
                        for image in images:
                            image.close()
                except Exception as exc:
                    logger.warning(f"Failed to merge table images {source_paths}: {exc}")
                    continue

            current_image_source = content.get("image_source")
            current_path = (
                current_image_source.get("path")
                if isinstance(current_image_source, dict)
                else None
            )
            if current_path != merged_relative_path:
                image_source = content.setdefault("image_source", {})
                if isinstance(image_source, dict):
                    image_source["path"] = merged_relative_path
                    changed = True

    return changed


def repair_content_list_v2(parse_dir: str, pdf_name: str) -> Optional[str]:
    """Build a repaired V2.1 JSON without modifying the original V2 JSON."""
    v2_path = os.path.join(parse_dir, f"{pdf_name}_content_list_v2.json")
    v21_path = os.path.join(parse_dir, f"{pdf_name}_content_list_v2_1.json")
    image_dir = os.path.join(parse_dir, "images")
    if not os.path.isfile(v2_path) or not os.path.isdir(image_dir):
        return None

    with open(v2_path, "r", encoding="utf-8") as fp:
        source_text = fp.read()

    image_list = []
    provenance = _middle_image_metadata(parse_dir, pdf_name)
    for filename in os.listdir(image_dir):
        file_path = os.path.join(image_dir, filename)
        if not os.path.isfile(file_path):
            continue
        item = {
            "path": f"images/{filename}",
            "name": filename,
            "size_bytes": os.path.getsize(file_path),
        }
        try:
            from PIL import Image
            with Image.open(file_path) as image:
                item["width"], item["height"] = image.size
        except Exception:
            pass
        records = provenance.get(filename) or [None]
        for record in records:
            enriched = dict(item)
            if record:
                enriched.update(record)
            image_list.append(enriched)

    matched = match_missing_images(source_text, image_list)
    patched_json = matched["patched_json"]
    repaired = bool(matched["unreferenced_images"] and matched["matches"])
    merged = merge_table_source_images(patched_json, parse_dir)
    if repaired or merged or not os.path.isfile(v21_path):
        patched_text = json.dumps(patched_json, ensure_ascii=False, indent=4)
        if patched_text != source_text or not os.path.isfile(v21_path):
            with open(v21_path, "w", encoding="utf-8") as fp:
                fp.write(patched_text)
        return patched_text
    return source_text



async def read_md_dump(
    output_image_path,
    content_list,
    content_list_version: str = "default",
)-> MagicPdfParseMainOutput: 

    # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亜鐓涢柛婊€鐒﹂弲顏堟偡濠婂嫬鐏村┑锛勬暬楠炲洭寮剁捄銊モ偓鐐差渻閵堝棗鍧婇柛瀣崌閺岋綁骞囬濠呭惈闂佸搫鏈惄顖炵嵁濡綍鏃堝焵椤掑嫬绐楁慨妯挎硾濮?

    images = []

    if os.path.exists(output_image_path):

        for filename in os.listdir(output_image_path):

            file_path = os.path.join(output_image_path, filename)

            if os.path.isfile(file_path):

                with open(file_path, 'rb') as file:

                    file_data = file.read()

                    output_image_path_url=output_image_path.replace("\\", "/")

                    url = f"{MAGIC_PDF_IMG_URL}/{output_image_path_url}/{filename}"

                    image_data = ImageData(name=filename, url=url)

                    images.append(image_data)



    de_content_list = json.loads(content_list)



    if content_list_version in {"v2", "v2_1"}:
        return MagicPdfParseMainOutput.model_construct(
            content_list=de_content_list,
            images=images,
        )
    return MagicPdfParseMainOutput(content_list=de_content_list, images=images)



def encode_image(image_path: str) -> str:

    # Encode image using base64

    with open(image_path, "rb") as f:

        return b64encode(f.read()).decode()



def get_infer_result(file_suffix_identifier: str, pdf_name: str, parse_dir: str) -> Optional[str]:

    # 婵犵數濮烽弫鎼佸磻濞戙埄鏁嬫い鎾跺枑閸欏繘鏌℃径瀣閻熸瑥瀚峰Σ褰掑箹缁厜鍋撳畷鍥﹀枈闂傚倷绶氬褑澧濋梺鍝勬噺閻╊垰鐣峰▎鎾村亹鐎规洖娲ㄩ惁鍫熺節閻㈤潧孝闁稿﹥鎮傞、鏃堫敂閸喓鍘介柟鍏兼儗閸犳牠寮稿☉銏☆梿濠㈣埖鍔栭悡鐔镐繆椤栨稑顕滈柣婵愪邯閺岋紕鈧綆浜滈顓㈡煛鐏炶鈧繂鐣疯ぐ鎺濇晝闁靛浚婢€閹綁姊绘担鍝ョШ妞ゃ儲鎸婚幈銊╂倻閽樺顔嗛梺缁橆焾椤曆呯不閻㈠憡鐓欓柣鎴炆戦埛鎰版煕鐎ｆ柨娲﹂埛鎺懨归敐鍥╂憘婵炲吋鍔曢湁婵犲ň鍋撶紒顔界懃閻ｅ嘲鈹戦崶銊ュ妳闂侀潧顭堥崕鎶芥偩閻戞绡€闁汇垽娼у瓭闁诲孩鍑归崰姘珶閺囩喓闄勯柛娑橈功閸樼敻姊虹拠鈥崇€婚悘鐐跺Г椤斿秴鈹?""

    result_file_path = os.path.join(parse_dir, f"{pdf_name}{file_suffix_identifier}")

    if os.path.exists(result_file_path):

        with open(result_file_path, "r", encoding="utf-8") as fp:

            return fp.read()

    return None



def normalize_suffix(file_suffix: str) -> str:

    return file_suffix.lower().lstrip(".")



def match_supported_suffix(file_suffix: str, supported_suffixes: list[str]) -> Optional[str]:

    normalized_suffix = normalize_suffix(file_suffix)

    print(f"文件后缀{normalized_suffix}")

    for supported_suffix in supported_suffixes:

        if normalize_suffix(supported_suffix) == normalized_suffix:

            return supported_suffix

    return None



def get_parse_dir(base_dir: str, pdf_name: str, backend: str, parse_method: str) -> str:

    if backend == "pipeline":

        parse_subdir = parse_method

    elif backend.startswith("hybrid-"):

        parse_subdir = f"hybrid_{parse_method}"

    else:

        parse_subdir = "vlm"

    return os.path.join(base_dir, pdf_name, parse_subdir)



async def magic_pdf_parse_main(

    file:UploadFile,

    parse_method: str="auto",

    is_save_local: bool=True,

    local_output_path: str=None,

    lang_list: list[str] = ["ch"],

    backend=DEFAULT_BACKEND,

    formula_enable=True,

    table_enable=True,

    server_url=None,

    return_md=True,

    return_middle_json=True,

    return_model_output=True,

    return_content_list=True,

    return_images=True,

    start_page_id=0,

    end_page_id=None,

    content_list_version: str="default",

    config={}

    ) ->BaseResultModel:


    # Parse an uploaded PDF or image and return generated outputs.


    content_list_suffixes = {
        "default": "_content_list.json",
        "v2": "_content_list_v2.json",
        "v2_1": "_content_list_v2_1.json",
    }
    if content_list_version not in content_list_suffixes:
        raise ValueError("content_list_version must be one of: default, v2, v2_1")

    result=BaseResultModel()

    try:

        backend = normalize_backend(backend)


        if not os.path.exists("temp_files"):

            os.makedirs("temp_files")

        if not local_output_path:

            local_output_path="temp_files"



        unique_dir = os.path.join(local_output_path, str(uuid.uuid4()))

        os.makedirs(unique_dir, exist_ok=True)



        # 婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐐茬劰闁稿鎸搁～婵嬫偂鎼淬垻褰庢俊銈囧Х閸嬫盯宕婊勫床婵犻潧顑呯粈鍐┿亜韫囧海鍔嶉柣蹇撳暙閳规垿鎮╅崹顐ｆ瘎婵犳鍠楁繛濠囧箠濡ゅ懎閱囬柡鍥╁枎閳ь剙鍢查埞鎴︽偐閸欏顦╅梺缁樻尵閸犳牠寮婚弴鐔虹闁割煈鍠栨慨搴♀攽閻愯尙澧㈢痪顓犲殝闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁?

        pdf_file_names = []

        pdf_bytes_list = []



        content = await file.read()

        file_path = Path(file.filename)

                
        file_suffix = match_supported_suffix(

            file_path.suffix.split(".")[1],

            office_suffixes+pdf_suffixes + image_suffixes,

        )



        # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘⒑瑜版帒浜伴柛鎾寸懃椤曪絽鐣￠柇锔藉瘜闂侀潧鐗嗗Λ娑欐櫠椤掑倻妫柡澶庢硶鑲栫紓渚囧枛椤戝鐣锋總绋课ㄩ柨鏃€鍎抽獮鍫ユ煟鎼达紕鐣柛搴ㄤ憾楠炲﹪骞囬鍙ョ瑝闂佸搫琚崕鏌ユ偂濞戙垺鍊甸柨婵嗙凹缁ㄥ鏌￠崱娆忎户缂佽鲸甯″畷鎺戔槈濡槒鐧侀梻浣侯攰濞呮洜鍒掗幘婢勬盯宕橀妸銏☆潔濠殿喗锕╅崑鍡涙偝缁摖F闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熺€电浠ч梻鍕閺岋繝宕橀敐鍛缂傚倷鑳剁划顖炴儎椤栨氨鏆﹂柛顐ｆ处閺佸棗霉閿濆娅滅紓鍌涙崌濮婄粯鎷呴崫銉︾€┑鐐叉嫅缂嶄礁顕ｉ锔衡偓鈧い顏嗗fn婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐐茬劰闁稿鎸搁～婵嬫偂鎼淬垻褰庢俊?

        if file_suffix is not None:


            temp_path = Path(unique_dir) / file_path.name

            with open(temp_path, "wb") as f:

                f.write(content)



            try:

                pdf_bytes = read_fn(temp_path, file_suffix=file_suffix)

                pdf_bytes_list.append(pdf_bytes)

                pdf_file_names.append(file_path.stem)

                os.remove(temp_path)  # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佺粯鍔曢顓犵不妤ｅ啯鐓冪憸婊堝礈濮樿鲸宕叉繛鎴欏灩瀹告繃銇勯幘璺哄壉闁告柨顦辩槐鎾存媴閸撴彃鍓板銈忕畵娴滃爼宕洪埀顒併亜閹哄秷鍏岄柍顖涙礋閹锋垶娼忛妸锝勭盎闁挎粌顭峰畷鍫曞Ω瑜嶉獮鎰版⒒娴ｄ警鐒鹃柡鍫墰閸犲﹤顓兼径瀣壒濠德板€撻懗鍓佺不?

            except Exception as e:

                logger.exception(f"Failed to load file: {str(e)}")

        

                result.code=500

                result.msg="processing error"

                return result

        else:

            logger.exception(f"Unsupported file type: {file_path.suffix}")

        

            result.code=500

            result.msg="processing error"

            return result



        # 闂傚倸鍊峰ù鍥х暦閸偅鍙忕€规洖娲ㄩ惌鍡椕归敐鍫綈婵炲懐濮撮湁闁绘ê妯婇崕鎰版煕鐎ｅ吀閭柡灞剧洴閸╁嫰宕橀鍛珬闂佽瀛╃喊宥囧枈瀹ュ桅闁告洦鍨扮粻缁樸亜閺嶃劋绶遍柍褜鍓欏Λ婵嬪蓟閿涘嫧鍋撻敐搴濈凹闁搞倕娲弻锛勪沪閸撗勫垱濡ょ姷鍋炵敮锟犵嵁濞嗘挸绀冮柍鍝勫暞濮ｅ矂姊婚崒娆戠獢婵炰匠鍏炬稑螖閸涱厾鏌堥梺鍦檸閸犳牠寮告担琛″亾楠炲灝鍔氶柟铏姍瀵娊宕卞☉娆戝帗閻熸粍绮撳畷婊堟偄閻撳骸鐎梺璇″灱閻忔盯鎮㈤崗鐓庢闂侀潧鐗嗛崐褰掑极閸偆绡€闁汇垽娼ч埢鍫熺箾娴ｅ啿娲ょ粻鐘绘煙閹殿喖顣奸柛瀣剁節閺屾洝绠涚€ｎ亖鍋撻弽顓熷亗婵炴垯鍨洪悡鏇㈡倶閻愭彃鈷旀い锝嗙叀閺岋綁顢橀悤浣圭暦缂備胶绮惄顖氱暦闁秴绠涙い鎺戝€风槐娆撴煟鎼淬埄鍟忛柛锝庡櫍瀹曟垿宕ㄩ妤€浜鹃梻鍫熺◤閸嬨垻鈧鍠栭悥濂哥嵁鐎ｎ喗鍊婚柛鈩冩礈閺佹儳鈹戦悩鍨毄闁稿鍋ゅ畷褰掑醇閺囩偟顔囬梺鍦檸閸犳牜澹曟繝姘厵闁告挆鍛闂?

        actual_lang_list = lang_list

        if len(actual_lang_list) != len(pdf_file_names):

            # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘煟韫囨洖浠﹂柛搴㈠▕閹敻寮跺▎鐐瘜闂侀潧鐗嗗Λ娆戜焊閵婏缚绻嗛柣娆愮懃濞层倝宕欓悩缁樼厱婵炴垶顭囬幗鐘绘煟閹惧瓨绀嬮柡宀€鍠栭幃婊兾熺紒妯哄壆婵犵數鍋涢悧濠囨偡閳哄懎钃熼柡鍥ュ灩楠炪垺淇婇妶鍌氫壕闂佸搫妫寸徊楣冨箞閵婏妇绡€闁稿本绮庢闁诲氦顫夊ú鏍礊婵犲洢鈧礁鈻庨幋婵囩€抽梺鍛婎殘閸嬬偤寮抽娑氱瘈闁汇垽娼ф禒锕傛煕閵娿儳鍩ｆ鐐村姍瀹曨偊濡疯閻撳鏌ｆ惔锝嗘毈闁哄懏鐓￠幃鐐寸節閸愶缚绨婚梺鍝勬处椤ㄥ懏绂嶆ィ鍐┾拺闁告繂瀚€氫即鏌ㄩ弴妯衡偓婵嬨€佸鈧幊婵嬪箥椤旂偓婢戦梻浣告惈濞层劍鍒婇鐐嶏絽鐣￠幍铏杸闂佺粯鍔橀崺鏍亹瑜忕槐鎾愁吋娴ｉ晲澹曞┑鐘垫暩閸嬫盯藝閺夋５娲偄婵傚娈ㄥ銈嗗姧缁犳垵娲块梻浣规偠閸庮噣寮插┑瀣仼閻犺桨璀﹀〒濠氭煏閸繃顥滈柣蹇氶哺缁绘稒寰勭€ｎ偆顦板銈冨灪濡啫鐣烽敓鐘冲€烽柍鍝勫亞濞兼棃姊绘担鐑樺殌闁诲繑绻堝畷顖炲煛娴煎崬缍婇弻銊р偓锝冨妺缁ㄥ姊洪崫鍕闁挎岸鏌ｈ箛锝呮珝闁哄瞼鍠栭幖褰掝敃閵堝棗鍨遍梻浣虹《閺備線宕戦幘鎰佹富闁靛牆妫楃粭鎺楁倵濮樼厧澧扮紒顔碱煼閺佹劖寰勭€ｎ剙骞愰柣搴＄畭閸庤鲸顨ラ幖浣哄祦婵せ鍋撻柟顔荤矙椤㈡稑鈽夊顓炲灡闂?ch"

            actual_lang_list = [actual_lang_list[0] if actual_lang_list else "ch"] * len(pdf_file_names)



        # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柟缁㈠櫘閺佸嫰鏌涘☉娆愮稇闁汇値鍠栭湁闁稿繐鍚嬬紞鎴︽煛鐎ｂ晝绐旈柡灞炬礋瀹曠厧鈹戦崶褜鈧盯姊洪幖鐐测偓鏍洪悢鐓庤摕婵炴垶鐭▽顏堟煕濞嗗秴鍔ょ紒璁崇窔濮婃椽宕ㄦ繝鍐弳闁诲孩鍑归崜鐔煎灳閿旂偓宕夐柕濠忕畱绾绢垶姊洪崨濠勭畵閻庢岸鏀辩€靛ジ鍩€椤掑嫭鈷掑ù锝囩摂閸ゅ啴鏌涢悩宕囧⒌闁轰礁鍟撮、鏃堝礋椤撶喐顔曟繝鐢靛仜濡瑩骞愭繝姘惞鐎广儱顦伴悡蹇擃熆閼哥數娲存俊缁㈠枟閵?

        await aio_do_parse(

            output_dir=unique_dir,

            pdf_file_names=pdf_file_names,

            pdf_bytes_list=pdf_bytes_list,

            p_lang_list=actual_lang_list,

            backend=backend,

            parse_method=parse_method,

            formula_enable=formula_enable,

            table_enable=table_enable,

            server_url=server_url,

            f_draw_layout_bbox=True,

            f_draw_span_bbox=True,

            f_dump_md=return_md,

            f_dump_middle_json=return_middle_json,

            f_dump_model_output=return_model_output,

            f_dump_orig_pdf=True,

            f_dump_content_list=return_content_list,

            start_page_id=start_page_id,

            end_page_id=end_page_id,

            **config

        )



        # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂缁樻櫈闂佸憡渚楅崹顏堝磻閹炬剚娼╅柣鎾抽椤偆绱撴担浠嬪摵闁圭懓娲悰顔碱潨閳ь剙鐣峰鍕闁绘艾顕惔濠囨⒒閸屾瑦绁版い顐㈩樀瀹曟洟骞庣粵瀣櫔濡炪倖鎸鹃崰鎾汇€呴崣澶堜簻闁哄稁鍋勬禒锕傛煕鐎ｅ墎鍒伴柕鍡樺笒椤繈鎮℃惔锝勫摋闂備浇顕х换鎺撴叏妞嬪孩顫?

        result_dict = {}

        for pdf_name in pdf_file_names:

            result_dict[pdf_name] = {}

            data = result_dict[pdf_name]



            parse_dir = get_parse_dir(unique_dir, pdf_name, backend, parse_method)



            if os.path.exists(parse_dir):

                repaired_content_list_v2 = repair_content_list_v2(parse_dir, pdf_name)

                if return_md:

                    data["md_content"] = get_infer_result(".md", pdf_name, parse_dir)

                if return_middle_json:

                    data["middle_json"] = get_infer_result("_middle.json", pdf_name, parse_dir)

                if return_model_output:

                    if backend.startswith("pipeline"):

                        data["model_output"] = get_infer_result("_model.json", pdf_name, parse_dir)

                    else:

                        data["model_output"] = get_infer_result("_model_output.txt", pdf_name, parse_dir)

                if return_content_list:

                    # V2 is page-grouped (List[List[Dict]]), while the API
                    # response model expects the legacy content-list shape.
                    # The repaired V2 file is persisted separately above.
                    data["content_list"] = get_infer_result(
                        content_list_suffixes[content_list_version],
                        pdf_name,
                        parse_dir,
                    )

                if return_images:

                    image_paths = glob(f"{parse_dir}/images/*.jpg")

                    data["images"] = {

                        os.path.basename(

                            image_path

                        ): f"data:image/jpeg;base64,{encode_image(image_path)}"

                        for image_path in image_paths

                    }



                # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢妶鍌氫壕婵ê宕崢瀵糕偓瑙勬礉椤鈧潧銈稿鍫曞箣閻樺灚姣庢繝鐢靛仩閹活亞寰婇崸妤€纾块柕鍫濐槸閻ゎ噣鏌涘┑鍡椻枙鐟滅増甯楅弲鏌ユ煕閵夈儱顣抽柛鏃€鎮傚铏规嫚閼碱剛鐣鹃梺鍝勬噽婵挳锝炶箛鏃傜瘈婵﹩鍓涢ˇ浼存⒑鐎圭姵銆冮悹浣圭叀瀹曟垿骞樼拠鎻掑祮闂佸疇娉涢幖顐⒚洪銏犳槬闁跨喓濮村婵囥亜閺冨牊锛熼柛婵囶殜濮婄粯鎷呴搹骞库偓濠囨煕閹惧绠氶柟绛嬪亰濮婅櫣鎷犻幓鎺旀О闂侀潻缍囩紞浣割嚕?

        result.data=await read_md_dump(
            f"{parse_dir}/images",
            data["content_list"],
            content_list_version=content_list_version,
        )



        return result

    

    except Exception as e:

        logger.exception(e)

        

        result.code=500

        result.msg="processing error"

        return result



async def magic_pdf_parse_main2(file:UploadFile,

    parse_method: str="auto",

    is_save_local: bool=False,

    local_output_path: str=None,

    lang_list: list[str] = ["ch"],

    backend=BACKEND_VLM_ENGINE,

    formula_enable=True,

    table_enable=True,

    server_url=None,

    return_md=True,

    return_middle_json=True,

    return_model_output=True,

    return_content_list=True,

    return_images=True,

    start_page_id=0,

    end_page_id=99999,

    config={}

    ) ->BaseResultModel:



    # Parse an uploaded PDF and write generated files to the target folder.



    result=BaseResultModel()

    try:

        backend = normalize_backend(backend)



        if not local_output_path:

            local_output_path = 'temp_files' # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亜鐓涢柛婊€鐒﹂弲顏堟偡濠婂嫬鐏村┑锛勬暬楠炲洭寮剁捄銊モ偓鐐差渻閵堝棗鍧婇柛瀣尰娣囧﹪顢曢敐蹇氣偓鍧楁煛鐏炲墽娲村┑锛勫厴椤㈡盯鎮欓幖顓涘亾瀹ュ拋娓婚柕鍫濇婵本淇婇銏狀伃闁炽儻绠撳畷绋课旈埀顒勬煁閸ヮ剚鐓涢柛銉厛濞堟柨霉濠婂懎浜剧紒缁樼洴楠炲鎮欓悽鐢靛帎婵°倗濮烽崑鎰板磻閹剧粯鈷掗柛灞剧懅閸斿秹鏌ｉ鍕Ш鐎规洘绻傞悾婵嬪焵椤掑嫬鐒垫い鎺嶇閸ゎ剟鏌涘Ο鍦煓鐎殿喛灏欓幑鍕瑹椤栨碍鍊┑鐘灱濞夋盯鏁冮妶澶嬪仧婵☆垵宕电弧鈧梺闈涢獜缁插墽娑甸悙顑句簻闁瑰瓨绻冮ˉ銏ゆ煙椤旂瓔娈滄い銏″哺閸┾偓妞ゆ帒瀚拑?





        pdf_name = file.filename



        name_without_suff = Path(pdf_name).stem



        #闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐣鹃梺鍛婃尰缁诲嫮妲愰悙鍝勭倞妞ゆ帊鑳堕崢鐢告⒑鐠団€崇€婚柛鎰ㄦ櫆閻︼絾绻濆▓鍨珯缂佽弓绮欒棟闁汇垻顭堥拑鐔兼煥濠靛棭妲搁幆鐔兼⒑闂堟侗妲堕柛搴ｅ劋鐎靛ジ鍩€椤掍胶绡€闁汇垽娼ф禒鈺冣偓娈垮枟閹瑰洤鐣烽敓鐘茬闁芥ê顦伴悗?

        output_path = os.path.join(local_output_path, name_without_suff)

        #闂傚倸鍊搁崐鐑芥倿閿曞倸绠栭柛顐ｆ礀绾惧潡寮堕崼娑樺婵炲懐濞€閺屻倝骞侀幒鎴濆濠电偛鎳愭繛鈧柡灞糕偓鎰佸悑闁告劑鍔岄‖澶岀磽閸屾氨孝婵炲樊鍙冨濠氭偄閻撳海鐣鹃悷婊冪Ч瀵櫕娼忛妸锕€寮块梺鎸庣箓閹峰螣閳ь剟姊洪崫鍕効缂傚秳鐒︽穱濠囧箹娴ｈ倽銊╂煏婢舵ê鏋欑紒杈ㄥ灦缁绘繈鎮介棃娑楀摋閻庢鍠楅幑鍥х暦閿熺姴绠柦妯侯槹閻?

        output_image_path = os.path.join(output_path, 'images')



        pdf_bytes = await file.read()  # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亜鐓涢柛婊€鐒﹂弲顏堟偡濠婂嫬鐏村┑?pdf 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐤勯梺绋匡攻閻楃姴鐣烽弴鐑嗗悑濠㈣泛顑囬崢閬嶆⒑閸濆嫭鍌ㄩ柛鏂款儑閼鸿鲸绂掔€ｎ偆鍘介悷婊冪Ч閹矂宕掑鐓庢闂佸壊鍋呭ú鏍偂濞戙垺鐓曢悘鐐村劤椤ユ碍淇婇幓鎺斿ⅵ婵﹨娅ｉ崠鏍即閻斿摜褰嗘俊鐐€戦崝宀勬晝閵堝鏁嬮柨婵嗩樈閺佸啴鏌ㄩ弮鍥棄濞存粓浜跺娲礈閹绘帊绨肩紓浣筋嚙鐎氱増淇?



        # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘⒑瑜版帒浜伴柛妯恒偢瀹曟粓顢欑喊杈ㄥ瘜闂侀潧鐗嗗Λ娆撳煕閹烘鐓涢柛婊€绀佹禍鎵偓瑙勬礉椤缂撴禒瀣窛濠电姴瀚獮妤呮⒒娴ｇ鏆遍柟纰卞亰閺佸啴濡舵径濠傚殤濠电偞鍨跺銊у閻ｅ备鍋撻獮鍨姎闁硅櫕鍔栭悧搴ｇ磽閸屾瑧顦︽い鎴濇嚇钘濇い鏍剱閺佸洤鈹戦崒婊庣劸鐎瑰憡绻冮妵鍕冀閵娧€濮囬梺鍝ュУ閻楃姴顫忕紒妯肩懝闁逞屽墮椤洭鎳￠妶鍌氫壕闂傚牊绋撻悞鎼佹煙椤旂懓澧茬€垫澘瀚禒锔剧磼閵忥紕绋愬┑鐘垫暩婵炩偓婵炰匠鍥舵晞闁告侗鍨抽惌鍡涙煕閳╁喚鐒界紒鐘荤畺閺屾盯鍩勯崘鐐暭缂備胶濮抽崡鎶藉蓟閿濆牏鐤€闁靛／鍜冪吹闂備線鈧偛鑻晶浼存煕鐎ｅ墎绉€规洘鍔曢埥澶娢熼柨瀣偓娲⒑閹稿海绠撻柟顔兼健閹粌螣娓氼垰娈奸梺璇插缁嬫帟鎽┑?

        if not os.path.exists(output_path):

            os.makedirs(output_path)

        

         #闂傚倸鍊搁崐鐑芥嚄閸洏鈧焦绻濋崒妤佺亙濠电偞鍨崹娲疾濠靛鐓忓璺烘濞呭懘鏌涢幋鐘测枅闁哄本鐩獮鍥Ω閿旂晫褰囨俊鐐€愰弲婵嬪礂濮椻偓楠炲啰鎲撮崟顒€顫￠梺鐟板槻閻牓宕濋崨顓涙斀闁斥晛鍟徊缁樸亜椤撶姴鍘寸€殿喛顕ч埥澶愬閻樻鍞洪梻浣告惈濞层劑宕伴幘璇参ラ柟鎵閳锋垹绱掗娑欑婵炲懎鎳橀弻锝夘敇閻愭祴鍋撻崸妤冨祦闁糕剝绋戠粈瀣亜閺嶇數绋婚柡鍛櫊濮婄儤瀵煎▎鎴濆煂闂佸吋妞块崹鍫曞箖濮椻偓閺屽棗顓奸崱蹇斿闂備礁鎲＄换鍌溾偓姘煎墴閸┾偓妞ゆ帒鍋嗛悞楣冩偂閵堝鐓ユ繝闈涙閸ｆ娊鏌＄€ｎ亪鍙勯柡灞炬礉缁犳稓鈧綆浜栭崑鎾诲冀椤撶偟鏌?

        copy_file_path=os.path.join(output_path,pdf_name)



        # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鑼槷闂佸搫绋侀崢浠嬪磻閿熺姵鐓忓璺烘濞呭懘鏌ｉ鐕佹疁闁哄本鐩崺鍕礃椤忎焦顫嶉梺璇插閼归箖藝娴兼潙桅闁告洦鍨扮粻鎶芥煕閳╁啨浠﹀瑙勬礃缁绘繈鎮介棃娴舵盯鏌涚€ｎ偅宕岄柡宀€鍠撶槐鎺楀閻樺磭浜繝纰樻閸嬪懘鏁冮姀銈呰摕婵炴垯鍨瑰敮濡炪倖鐗滈崑鐐侯敁瀹ュ拋娓?

        with open(copy_file_path, "wb") as f:

            f.write(pdf_bytes)



        # 婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐐茬劰闁稿鎸搁～婵嬫偂鎼淬垻褰庢俊銈囧Х閸嬫盯宕婊勫床婵犻潧顑呯粈鍐┿亜韫囧海鍔嶉柣蹇撳暙閳规垿鎮╅崹顐ｆ瘎婵犳鍠楁繛濠囧箠濡ゅ懎閱囬柡鍥╁枎閳ь剙鍢查埞鎴︽偐閸欏顦╅梺缁樻尵閸犳牠寮婚弴鐔虹闁割煈鍠栨慨搴♀攽閻愯尙澧㈢痪顓犲殝闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁?

        pdf_file_names = []

        pdf_bytes_list = []





        pdf_bytes_list.append(pdf_bytes)

        pdf_file_names.append(Path(copy_file_path).stem)

        



        # 闂傚倸鍊峰ù鍥х暦閸偅鍙忕€规洖娲ㄩ惌鍡椕归敐鍫綈婵炲懐濮撮湁闁绘ê妯婇崕鎰版煕鐎ｅ吀閭柡灞剧洴閸╁嫰宕橀鍛珬闂佽瀛╃喊宥囧枈瀹ュ桅闁告洦鍨扮粻缁樸亜閺嶃劋绶遍柍褜鍓欏Λ婵嬪蓟閿涘嫧鍋撻敐搴濈凹闁搞倕娲弻锛勪沪閸撗勫垱濡ょ姷鍋炵敮锟犵嵁濞嗘挸绀冮柍鍝勫暞濮ｅ矂姊婚崒娆戠獢婵炰匠鍏炬稑螖閸涱厾鏌堥梺鍦檸閸犳牠寮告担琛″亾楠炲灝鍔氶柟铏姍瀵娊宕卞☉娆戝帗閻熸粍绮撳畷婊堟偄閻撳骸鐎梺璇″灱閻忔盯鎮㈤崗鐓庢闂侀潧鐗嗛崐褰掑极閸偆绡€闁汇垽娼ч埢鍫熺箾娴ｅ啿娲ょ粻鐘绘煙閹殿喖顣奸柛瀣剁節閺屾洝绠涚€ｎ亖鍋撻弽顓熷亗婵炴垯鍨洪悡鏇㈡倶閻愭彃鈷旀い锝嗙叀閺岋綁顢橀悤浣圭暦缂備胶绮惄顖氱暦闁秴绠涙い鎺戝€风槐娆撴煟鎼淬埄鍟忛柛锝庡櫍瀹曟垿宕ㄩ妤€浜鹃梻鍫熺◤閸嬨垻鈧鍠栭悥濂哥嵁鐎ｎ喗鍊婚柛鈩冩礈閺佹儳鈹戦悩鍨毄闁稿鍋ゅ畷褰掑醇閺囩偟顔囬梺鍦檸閸犳牜澹曟繝姘厵闁告挆鍛闂?

        actual_lang_list = lang_list

        if len(actual_lang_list) != len(pdf_file_names):

            # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘煟韫囨洖浠﹂柛搴㈠▕閹敻寮跺▎鐐瘜闂侀潧鐗嗗Λ娆戜焊閵婏缚绻嗛柣娆愮懃濞层倝宕欓悩缁樼厱婵炴垶顭囬幗鐘绘煟閹惧瓨绀嬮柡宀€鍠栭幃婊兾熺紒妯哄壆婵犵數鍋涢悧濠囨偡閳哄懎钃熼柡鍥ュ灩楠炪垺淇婇妶鍌氫壕闂佸搫妫寸徊楣冨箞閵婏妇绡€闁稿本绮庢闁诲氦顫夊ú鏍礊婵犲洢鈧礁鈻庨幋婵囩€抽梺鍛婎殘閸嬬偤寮抽娑氱瘈闁汇垽娼ф禒锕傛煕閵娿儳鍩ｆ鐐村姍瀹曨偊濡疯閻撳鏌ｆ惔锝嗘毈闁哄懏鐓￠幃鐐寸節閸愶缚绨婚梺鍝勬处椤ㄥ懏绂嶆ィ鍐┾拺闁告繂瀚€氫即鏌ㄩ弴妯衡偓婵嬨€佸鈧幊婵嬪箥椤旂偓婢戦梻浣告惈濞层劍鍒婇鐐嶏絽鐣￠幍铏杸闂佺粯鍔橀崺鏍亹瑜忕槐鎾愁吋娴ｉ晲澹曞┑鐘垫暩閸嬫盯藝閺夋５娲偄婵傚娈ㄥ銈嗗姧缁犳垵娲块梻浣规偠閸庮噣寮插┑瀣仼閻犺桨璀﹀〒濠氭煏閸繃顥滈柣蹇氶哺缁绘稒寰勭€ｎ偆顦板銈冨灪濡啫鐣烽敓鐘冲€烽柍鍝勫亞濞兼棃姊绘担鐑樺殌闁诲繑绻堝畷顖炲煛娴煎崬缍婇弻銊р偓锝冨妺缁ㄥ姊洪崫鍕闁挎岸鏌ｈ箛锝呮珝闁哄瞼鍠栭幖褰掝敃閵堝棗鍨遍梻浣虹《閺備線宕戦幘鎰佹富闁靛牆妫楃粭鎺楁倵濮樼厧澧扮紒顔碱煼閺佹劖寰勭€ｎ剙骞愰柣搴＄畭閸庤鲸顨ラ幖浣哄祦婵せ鍋撻柟顔荤矙椤㈡稑鈽夊顓炲灡闂?ch"

            actual_lang_list = [actual_lang_list[0] if actual_lang_list else "ch"] * len(pdf_file_names)



        # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柟缁㈠櫘閺佸嫰鏌涘☉娆愮稇闁汇値鍠栭湁闁稿繐鍚嬬紞鎴︽煛鐎ｂ晝绐旈柡灞炬礋瀹曠厧鈹戦崶褜鈧盯姊洪幖鐐测偓鏍洪悢鐓庤摕婵炴垶鐭▽顏堟煕濞嗗秴鍔ょ紒璁崇窔濮婃椽宕ㄦ繝鍐弳闁诲孩鍑归崜鐔煎灳閿旂偓宕夐柕濠忕畱绾绢垶姊洪崨濠勭畵閻庢岸鏀辩€靛ジ鍩€椤掑嫭鈷掑ù锝囩摂閸ゅ啴鏌涢悩宕囧⒌闁轰礁鍟撮、鏃堝礋椤撶喐顔曟繝鐢靛仜濡瑩骞愭繝姘惞鐎广儱顦伴悡蹇擃熆閼哥數娲存俊缁㈠枟閵?

        await aio_do_parse(

            output_dir=output_path,

            pdf_file_names=pdf_file_names,

            pdf_bytes_list=pdf_bytes_list,

            p_lang_list=actual_lang_list,

            backend=backend,

            parse_method=parse_method,

            formula_enable=formula_enable,

            table_enable=table_enable,

            server_url=server_url,

            f_draw_layout_bbox=True,

            f_draw_span_bbox=True,

            f_dump_md=return_md,

            f_dump_middle_json=return_middle_json,

            f_dump_model_output=return_model_output,

            f_dump_orig_pdf=True,

            f_dump_content_list=return_content_list,

            start_page_id=start_page_id,

            end_page_id=end_page_id,

            **config

        )



        # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂缁樻櫈闂佸憡渚楅崹顏堝磻閹炬剚娼╅柣鎾抽椤偆绱撴担浠嬪摵闁圭懓娲悰顔碱潨閳ь剙鐣峰鍕闁绘艾顕惔濠囨⒒閸屾瑦绁版い顐㈩樀瀹曟洟骞庣粵瀣櫔濡炪倖鎸鹃崰鎾汇€呴崣澶堜簻闁哄稁鍋勬禒锕傛煕鐎ｅ墎鍒伴柕鍡樺笒椤繈鎮℃惔锝勫摋闂備浇顕х换鎺撴叏妞嬪孩顫?

        for pdf_name in pdf_file_names:

            parse_dir = get_parse_dir(output_path, pdf_name, backend, parse_method)



            if os.path.exists(parse_dir):

                # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢妶鍌氫壕婵ê宕崢瀵糕偓瑙勬礉椤鈧潧銈稿鍫曞箣閻樺灚姣庢繝鐢靛仩閹活亞寰婇崸妤€纾块柕鍫濐槸閻ゎ噣鏌涘┑鍡椻枙鐟滅増甯楅弲鏌ユ煕閵夈儱顣抽柛鏃€鎮傞铏规嫚閼碱剛鐣鹃梺鍝勬噽婵挳锝炶箛鏃傜瘈婵﹩鍓涢ˇ浼存⒑鐎圭姵銆冮悹浣圭叀瀹曟垿骞樼拠鎻掍壕闂佹眹鍨藉褔鍩㈤崼鐔虹濞达絽鍟垮ú銈囩不閻樼粯鐓欓柟娈垮枛椤ｅジ鏌涚€ｅ墎绡€闁哄本娲濈粻娑氣偓锝庝邯閸欏嫰鏌＄仦鐐缂佺姵绋撻埀顒婄秵娴滄牠宕戦幘璇插唨妞ゆ挾鍋熼弻褍顪冮妶鍡楃瑨閻庢凹鍙冮幃锟犲即閻旂繝绨婚梺瑙勬緲婢у酣骞冮懖鈺冪＜闁绘﹩鍠栭崝婊呯磼缂佹銆掑ù鐙呯畵瀹曟帒顫濋敐鍛濠电娀娼ч鍛存嫅閻斿吋鐓ユ繛鎴灻銈夋煕鐎ｎ偅宕岄柡浣瑰姈閹棃鍨鹃懠顒佹櫦闂傚倷鐒﹀鍧楀储婵傚壊鏁勯柛鈩冾焽閳瑰秴鈹戦悩鍙夋悙闁活厽顨呴…璺ㄦ崉娓氼垰鍓伴梺閫炲苯澧柛鏃€顨婇崺鈧い鎺嶇贰閸熷繘鏌涢敐搴℃珝鐎规洘绮撻幃銏☆槹鎼淬垺顔曢梻浣稿閸嬫懎煤濮椻偓瀵彃顭ㄩ崨顖滐紲濠电偞鍨堕敃鈺呭磿韫囨拋褰掓偐閾忣偁浠㈠┑?

                repair_content_list_v2(parse_dir, pdf_name)

                result_file_path = os.path.join(parse_dir, f"{pdf_name}")

                if os.path.exists(result_file_path):

                    shutil.rmtree(result_file_path)



                # 婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐐寸殤闁活厽鎹囬弻鐔虹磼閵忕姵鐏堥梺?parse_dir 婵犵數濮烽弫鎼佸磻閻愬搫鍨傞柛顐ｆ礀缁犲綊鏌嶉崫鍕櫣闁稿被鍔戦弻鐔碱敍閸″繐浜鹃梺鍝勵儐濡啴寮婚悢鍛婄秶闁告挆鍛缂傚倷鑳舵慨浼村磿閻㈢钃熺€广儱鐗滃銊╂⒑閸涘﹥灏甸柛鐘查叄椤㈡岸鏁愭径濠傜€銈嗗姂閸ㄨ崵绮ｉ悙瀵哥瘈闁汇垽娼у瓭闂佺锕ょ紞濠傤嚕閹惰棄唯闁冲搫鍊婚崢浠嬫煙閼测晞藟闁告挻绻勯幏褰掓晸閻樺磭鍘甸梺鎯ф禋閸嬪懎鐣峰畝鈧埀?output_path

                for item in os.listdir(parse_dir):

                    src = os.path.join(parse_dir, item)

                    dst = os.path.join(output_path, item)

                    

                    # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘⒑瑜版帒浜伴柛妯恒偢瀹曟粓顢欑喊杈ㄥ瘜闂侀潧鐗嗗Λ娆撳煕閹烘鐓涢柛婊€绀佹禍鎵偓瑙勬礉椤缂撴禒瀣窛濠电姴瀚獮鎰版⒒娴ｄ警鐒鹃柡鍫墰閸犲﹤顓兼径瀣壒濠德板€撻懗鍓佺不妤ｅ啯鐓曢柍鈺佸暙婵洭鏌ｈ箛濠冩珕闁靛洤瀚板顒傛崉閵娧屽晪婵°倗濮烽崑鐐哄箲閸ヮ剛宓侀柟鐑橆殔缁秹鏌嶈閸撶喖鐛径瀣檮闁告稑艌閹锋椽姊婚崒姘卞闁哄懏鐩幆浣割煥閸喓鍘介梺鍦亾閸撴艾危閻戞ü绻嗛柛娆忣槸婵洭鏌嶇憴鍕仼闁逞屽墾缂嶅棙绂嶉悙鐑樺亗闁瑰瓨绻嶅〒濠氭煏閸繃顥犲褜鍓熼弻锝夋偄閺夋垟鍋撳Δ鍛殟閺夊牄鍔岄閬嶆倵濞戞姘跺箯閸濆嫷娓婚柕鍫濇婵倿鏌涢埡浣割仼閸楄京鎲搁悧鍫濈瑲闁稿﹤鐏氶幈銊ノ熺粙鍨婵犵鈧櫕鍋ラ柡宀€鍠栭悡顐︻敇閻愯尙銈柣搴ゎ潐濞叉牕鐣烽鍕厺閹兼番鍔岀粻娑欍亜閺囩偞顥犻柣顓燁殜濮婂宕掑▎鎺戝帯闂佺娅曢幑鍥箖濞差亜惟闁冲搫鍊告禍閬嶆⒑缂佹ê濮夐柛搴涘€濆畷鎰節濮橆厾鍙嗛梺鍝勫暙濞层倛顣垮┑鐐茬摠閸ゅ酣宕愬┑瀣摕婵炴垯鍨瑰敮闂佸啿鎼敃銉┧夐弽顐ょ＝濞达絾褰冩禍楣冩⒑閸涘﹥澶勯柛銊﹀閻ヮ亣顦归柡灞界Ч瀹曨偊宕熼鐔蜂壕濠电姵鑹剧粻鐘绘煕閵夘喖澧柣鎾存礋閺屻劌鈹戦崱妯绘倷闂佸憡蓱閹告娊寮婚敍鍕勃闁兼亽鍎卞▍銈夋倵閸偅绶查悗姘煎櫍閸┾偓妞ゆ帒锕︾粔闈浢瑰鍡楃厫缂佸倹甯￠獮姗€顢欓悾灞藉箺闂備胶绮弻銊╁箟閿涘嫮鐭嗛柍褜鍓欓埞鎴﹀煡閸℃ぞ绨肩紓浣割儐閸ㄥ綊宕ｉ崨顔剧瘈闁汇垽娼у瓭濠电偠顕滅粻鎾愁嚕?

                    if os.path.exists(dst):

                        if os.path.isdir(dst):

                            shutil.rmtree(dst)  # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佺粯鍔曢顓犵不妤ｅ啯鐓冪憸婊堝礈濮樿鲸宕叉繛鎴欏灩瀹告繃銇勯幘鍗炵仼鐎殿喕鍗冲铏规嫚閳ュ磭浠╅柣搴㈢煯閸楁娊濡存担绯曟瀻闁圭偓娼欐禒濂告煟韫囨洖浠ч柡鍜佸亝鐎靛ジ宕堕浣叉嫽婵炶揪绲介幉锟犲箚閸儲鐓曞┑鐘插閻掗箖鎮￠妶澶嬬叆婵犻潧妫欓崳鎶芥煛鐎ｎ亪鍙勯柡灞炬礉缁犳稒绻涢幆褌澹曢悗瑙勬礀濞层劑鎮伴鈧缁樻媴閻ｅ本鐦掗梺鍛婂姀閺呮粓宕伴幇顔剧＝濞达絾褰冩禍楣冩⒑閸涘﹥澶勯柛瀣у亾闂佽　鍋撳ù鐘差儐閻撳啴鏌曟径鍫濆姎闁哄棝浜堕弻宥堫檨闁告挻鐟х槐鐐寸節閸パ呯暫濠碘槅鍨甸褏娆㈤悙鐑樼厵闂侇叏绠戞晶顔锯偓娈垮枛閸㈡煡鈥旈崘顔嘉ч柛鈩冪懃椤呯磽娓氬洤鏋涚紒澶婂閸掓帡鏁愰崨鍌涙瀹曟﹢鍩℃担鍦偓楣冩⒒娴ｈ櫣甯涙い銊ユ嚇閹勭節閸曨厾鐓旈柣鐘充航閸斿海澹?

                        else:

                            os.remove(dst)  # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佺粯鍔曢顓犵不妤ｅ啯鐓冪憸婊堝礈濮樿鲸宕叉繛鎴欏灩瀹告繃銇勯幘鍗炵仼鐎殿喕鍗冲铏规嫚閳ュ磭浠╅柣搴㈢煯閸楁娊濡存担绯曟瀻闁圭偓娼欐禒濂告煟韫囨洖浠ч柡鍜佸亝鐎靛ジ宕堕浣叉嫽婵炶揪绲介幉锟犲箚閸儲鐓曞┑鐘插閻掗箖鎮￠妶澶嬬叆婵犻潧妫欓崳鎶芥煛鐎ｎ亪鍙勯柡灞炬礉缁犳稓鈧綆浜栭崑鎾诲冀椤撶偟鏌у銈嗗笒鐎氼參鎮￠弴鐔翠簻闁规澘澧庨幃鑲╃磼閻橆喖鍔滅紒缁樼〒娴狅箓宕掑锝呬壕婵犻潧顑呴拑鐔兼煥濞戞ê顏ら柛瀣崌閺佹劖鎯旈垾鑼晼闂備焦妞块崢濂割敄閸℃鈹嶅┑鐘叉搐閻顭跨捄渚剰闁逞屽墯閸旀鍩€椤掍緡鍟忛柛锝庡櫍瀹曟粓鎮㈡搴㈡闂佸壊鍋呭ú姗€寮插鍛＜婵炴垶锕╅崕蹇撁归悩铏仢婵?

                    

                    if os.path.isdir(src):

                        shutil.copytree(src, dst)  # 闂傚倸鍊搁崐鎼佸磹妞嬪孩顐介柨鐔哄Т绾惧鏌涘☉鍗炵仭闁哄棙绮撻弻鐔兼倻濡儵鎷荤紓浣插亾濠电姴鍊甸弨浠嬫煟濡绲婚柡鍡樼懅缁辨帡宕滄担闀愭埛闂侀€炲苯澧紒鐘茬Ч瀹曟洟鏌嗗鍛枃闁瑰吋鐣崝宥夊磻閸岀偞鐓欓弶鍫ョ畺濡绢噣鏌ｉ幘璺烘灈闁哄苯绉瑰畷顐﹀礋鐠鸿桨绱ｉ梻浣告惈椤戝棛绮欓幘鑸殿潟?

                    else:

                        shutil.copy2(src, dst)  # 婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐐寸殤闁活厽鎹囬弻鐔虹磼閵忕姵鐏堥梺鍝勫閸庡弶绌辨繝鍥ч柛娑卞枛濞咃絽鈹戦埥鍡椾簼闁挎洏鍨藉璇测槈閵忕姈銊︺亜閺嶎偄浠︽い搴＄Т椤啴濡堕崱妤€顫戞繝娈垮枔閸婃繈濡撮崘顔嘉ㄩ柍鍝勫€搁埀顒傚厴閹鈽夊▍铏灴閹顢涘鍛紳婵炶揪缍€濞咃絿鏁☉姘ｅ亾閸忓浜鹃梺褰掓？缁€浣虹不閺嶎厽鐓欐い鏍ф閸嬫捇鏌＄€ｎ亪鍙勯柣鎿冨亰瀹曞爼濡搁敃鈧喊宥囩磽娴ｈ娈曠€光偓缁嬫娼栭柣鎴炆戞慨婊堟煙瀹勬媽瀚伴柛妯峰墲缁绘繈鍩涢埀顒勫礋閸偆鏆ラ梻?

                

                # 缂傚倸鍊搁崐鎼佸磹閻戣姤鍤勯柛鎾茶閸嬫挸顫濋鍌滎啋閻庤娲熸禍璺侯嚕閹绢喗鍋愰柣銏ゆ涧鐢劑姊婚崒姘偓椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佺粯鍔曢顓犵不妤ｅ啯鐓冪憸婊堝礈濮樿鲸宕?parse_dir

                shutil.rmtree(parse_dir)



                # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佺粯鍔曢顓犵不妤ｅ啯鐓冪憸婊堝礈濮樿鲸宕叉繛鎴欏灩瀹告繃銇勯幘璺哄壉闁告柨顦甸幃妤呭垂椤愶絿鍑￠柣搴㈠嚬閸撶喖銆佸Ο鑽ら檮缂佸鐏濈粣娑橆渻閵堝棙灏靛┑顔芥尦閻涱噣鍩€椤掑嫭鈷掗柛灞剧懅閸斿秹鏌ｉ鍕Ш鐎规洘绻傞悾婵嬪焵椤掑嫬鐒垫い鎺嶇閸ゎ剟鏌涘▎蹇撴殭妞ゆ洩缍侀獮鏍ㄦ媴閸濄儺妲伴梻浣稿暱閹碱偊宕板顑炲綊宕熼娑氬幗闂佺粯锕㈠褔宕濈€ｎ€㈢懓顭ㄩ崱妞ユ挾绱?

                result_file_path = os.path.join(output_path, f"{pdf_name}")

                if os.path.exists(result_file_path):

                    shutil.rmtree(result_file_path)



        return result

    

    except Exception as e:

        logger.exception(e)

        

        result.code=500

        result.msg="processing error"

        return result





#folder_paths闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佸湱鍎ら崵锕€鈽夊Ο婊勬瀹曠喖鍩￠埀顒€效濡ゅ懏鈷戠紒澶婃鐎氬嘲鈻撻弮鍌滅＜闁逞屽墴瀹曞崬鈽夊▎鎴濆箞闂備焦鏋奸弲娑㈠疮娴兼潙鐓€闁哄洨鍠嗘禍?

async def magic_pdf_parse_main_batch_all(folder_paths:str="",

    parse_method: str="auto",

    lang_list: list[str] = ["ch"]):

    paths = folder_paths.split(";")

    for folder_path in paths:

        await magic_pdf_parse_main_batch(folder_path=folder_path, parse_method=parse_method, lang_list=lang_list)

    

    



async def magic_pdf_parse_main_batch(

    folder_path:str="",

    parse_method: str="auto",

    lang_list: list[str] = ["ch"]

    ) ->BaseResultModel:

    result=BaseResultModel()



    if not os.path.exists("batch_files"):

        os.makedirs("batch_files")





    print("status")

    if not folder_path:

        folder_path = 'batch_files' # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亜鐓涢柛婊€鐒﹂弲顏堟偡濠婂嫬鐏村┑锛勬暬楠炲洭寮剁捄銊モ偓鐐差渻閵堝棗鍧婇柛瀣尰娣囧﹪顢曢敐蹇氣偓鍧楁煛鐏炲墽娲村┑锛勫厴椤㈡盯鎮欓幖顓涘亾瀹ュ拋娓婚柕鍫濇婵本淇婇銏狀伃闁炽儻绠撳畷绋课旈埀顒勬煁閸ヮ剚鐓涢柛銉厛濞堟柨霉濠婂懎浜剧紒缁樼洴楠炲鎮欓悽鐢靛帎婵°倗濮烽崑鎰板磻閹剧粯鈷掗柛灞剧懅閸斿秹鏌ｉ鍕Ш鐎规洘绻傞悾婵嬪焵椤掑嫬鐒垫い鎺嶇閸ゎ剟鏌涘Ο鍦煓鐎殿喛灏欓幑鍕瑹椤栨碍鍊┑鐘灱濞夋盯鏁冮妶澶嬪仧婵☆垵宕电弧鈧梺闈涢獜缁插墽娑甸悙顑句簻闁瑰瓨绻冮ˉ銏ゆ煙椤旂瓔娈滄い銏″哺閸┾偓妞ゆ帒瀚拑?



    print("status")

        # 闂傚倸鍊搁崐椋庣矆娓氣偓瀹曘儳鈧綆鍠栫壕鍧楁煙閹増顥夐幖鏉戯躬閺屻倝鎳濋幍顔肩墯婵炲瓨绮岀紞濠囧蓟濞戙垹唯妞ゆ棁宕甸弳妤佺箾鐎涙鐭婄紓宥咃躬瀵鎮㈤悡搴ｇ暰閻熸粌绉瑰铏綇閵婏絼绨婚梺闈涚墕閹冲繘宕甸崶顒佺厸鐎光偓鐎ｎ剛袦闂佺硶鏂侀崜婵堟崲濠靛纾兼繛鎴炵煯闁垳绱撻崒姘偓鎼佸磹閸濄儳鐭撻悗闈涙憸閻捇鏌ｉ姀銏℃毄闁活厽鐟ラ…璺ㄦ崉閸濆嫷妲甸梺绋款儐閹瑰洭寮幇顓熷劅婵犻潧鐗忓▔鍧楁⒒娴ｈ鍋犻柛濠冪墱閺侇噣骞掑鐑╁亾閿旂偓宕夐柕濠忕畱绾绢垶姊洪崨濠勭畵閻庢岸鏀辩€靛ジ鍩€椤掑嫭鈷掑ù锝囩摂閸ゅ啴鏌涢悩宕囧⒌闁轰礁鍟撮、鏃堝礋闂堟稒顓挎俊鐐€栫敮鎺斺偓姘煎墴閹瑦绻濋崶銊у弳闂佸搫鍟崐濠氬箺閸岀偞鐓曢悗锝冨妼婵倹鎱ㄦ繝鍌ょ吋鐎规洘甯掗～婵嬵敇瑜庨ˉ锝嗙節绾版ɑ顫婇柛瀣嚇閵嗗啯绻濋崶鈺佺ウ闂佸憡鍔忛弲婊堝磿閻斿吋鐓忓┑鐘茬箻濡绢喗绻涢崨顔惧⒌闁哄矉缍€缁犳盯寮撮悙鐗堝煕闂備礁鎼幏瀣磻婵犲洨宓佹俊銈呮噺閸嬫劗绱撴担璇＄劷闁告﹢浜堕弻锝堢疀閺囩偘鍝楀銈嗘肠閸曨亞绠氬┑鐐叉▕娴滄繈宕?{name_without_suff: path}

    processed_files = set()

    for root, _, files in os.walk(folder_path):

        for file_name in files:

            if file_name.endswith("_layout.pdf"):

                processed_files.add(file_name.replace("_layout.pdf", ""))





    upload_files: List[UploadFile] = []

    # 闂傚倸鍊搁崐鎼佸磹妞嬪孩顐介柨鐔哄Т缁€鍫熺箾閸℃ê鐏╅柣顓熸崌閺屸剝寰勭€ｎ亞鍔搁梺鍝ュ枎閹虫劗妲愰幒妤婃晝闁挎繂妫欓崯绱介梻鍌氬€搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐤勯梺绋匡攻椤ㄥ懘鎮鹃崹顐ょ懝闁逞屽墴瀵鎮㈢喊杈ㄦ櫖濠电偞鍨堕敃鈺佄涢崱娆戠＝濞达絽鎼牎闂佺粯顨嗛〃鍫ュ箲閵忕姭鏀介柛銉㈡櫇椤旀洟姊洪崜鑼帥闁哥姵甯″畷鎴﹀箻鐠囨彃鍞ㄩ悷婊勭箞閹虫捇骞愭惔娑楃盎闂佽宕樺▔娑㈠几鎼达絿纾奸柛鎾茬娴犻亶鏌＄仦鍓ф创鐎殿喗鎸虫俊鎼佸Ψ閵壯屽晥闂傚倷鑳堕…鍫ユ晝閿曞倸纾婚柕鍫濐槸閽冪喖鏌ㄥ┑鍡╂Ц閹喖姊洪棃娑辨Ф闁稿海鍎ょ粋鎺撱偅閸愨斁鎷洪梺鍛婄箓鐎氱兘宕曡箛娑欑厱闁绘柨鎲＄亸锔锯偓?

    for root, _, files in os.walk(folder_path):

        for file_name in files:

            # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐤勯梺绋匡攻濞叉粓骞夐幘顔芥櫆闂佹鍨版禍鐐殽閻愯尙浠㈤柛鏃€宀搁弻鐔煎礃閸欏宕崇紓渚囧枛椤兘骞冩禒瀣窛濠电偟鍋撶€氫粙姊绘担鍛婂暈婵炲弶鐗犻幃妯侯潩鐠佸湱绋忔繝闈涱槺閳锋悮out.pdf闂傚倸鍊搁崐鐑芥嚄閸洖绠犻柟鍓х帛閸嬨倝鏌曟繛鐐珕闁稿顑夐弻锟犲炊閵夈儳浠奸梺娲诲幗椤ㄥ﹪寮婚敐澶婄疀闂傚牊绋戦～顐㈩渻閵堝倹娅囬柛蹇旓耿瀵鍨惧畷鍥ㄦ濡炪倖姊婚崢褔寮冲▎鎴炲枑闁绘鐗忛幊鍥ㄦ叏婵犲洨绱伴柕鍥ㄥ姍楠炴帡骞橀幘顔芥殬闂備礁婀遍崢褔鎮洪妸褍鍨濋幖绮瑰灳閿濆绠涙い鎴ｅГ閺傗偓闂佽鍑界紞鍡涘磻閸℃稑绀夐柛顐ｆ礃閳锋垿鏌涢幘鐟扮毢闁告ɑ鐩弻娑㈡偐瀹曞洤鈷堥梺杞扮贰閸ｏ綁鐛惔銊﹀殟闁靛鍎伴崠鏍⒒娴ｈ鍋犻柛搴㈡綑閳绘柨鈽夐姀鈥斥偓鍫曟煟濡厧浠哄ù婊勭矒閺屻劑寮崶鑸电秷濠电偛鎳庨敃顏堝蓟閻旂⒈鏁婇柤娴嬫櫅閻撶喖鎮楃憴鍕鐎规洦鍓濋悘鎺楁⒑閻撳寒娼熼柛濠冨姍瀹曟垿骞樺ú缁樻櫔闂侀€炲苯澧寸€?

            if file_name.lower().endswith(".pdf") and "_layout" not in file_name and  "_origin" not in file_name:

                name_without_suff = Path(file_name).stem

                if name_without_suff not in processed_files:

                    file_path = os.path.join(root, file_name)

                    with open(file_path, "rb") as file:

                        file_data = file.read()

                        upload_file = UploadFile(file=io.BytesIO(file_data), filename=file_name)

                        upload_files.append(upload_file)

                else:

                    print("status")


    print("status")

    error_file=[]

    total=len(upload_files)

    index=1

    for upload_file in upload_files:

        print("status")

        magic_pdf_parse_main_result = await magic_pdf_parse_main2(upload_file, parse_method, True, folder_path, lang_list=lang_list)

        if magic_pdf_parse_main_result.code != 200:

            error_file.append(upload_file.filename)

            print("status")

        else:

            print("status")



        index=index+1



    compelete_str = "闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块梺顒€绉撮弸渚€鏌熼梻瀵割槮缂佺姷濞€閺岀喖骞嗚濞堟椽鏌涢妷顔煎闁绘挻鐩弻娑樷槈閸楃偞鐏嶅┑?闂傚倸鍊峰ù鍥敋瑜嶉湁闁绘垼妫勭粻鐘绘煙閹规劦鍤欓悗姘槹閵囧嫰骞掗幋婵愪患闂佹悶鍔岄崐褰掑箞閵娿儺娼ㄩ柛鈩冾殔缁犲湱绱撴担绋款暢闁稿鍊濆璇测槈閵忕姴宓嗛梺闈涱焾閸庤京绮诲ú顏呪拺缂佸灏呴崝鐔兼煕鐎ｃ劌鈧繂顕ｆ繝姘櫢闁绘ɑ鐓￠崬璺侯渻閵堝棗濮傞柛銊ョ秺閿濈偤鍩℃担鍙夋杸闂佺粯锕╅崑鍕妤ｅ啯鈷戦柛锔诲弨濡炬悂鏌涢悩宕囧ⅹ閾荤偞鎱ㄥ璇蹭壕闂佸搫鏈惄顖涗繆閻戠瓔鏁嶉柣鎰儗閳ь剙绉撮—鍐Χ閸℃顫堢紓渚囧枟閻熲晛顕?"

    if len(error_file) > 0:

        compelete_str = compelete_str + f"婵犵數濮烽弫鍛婃叏娴兼潙鍨傛繛宸簻绾惧潡鏌ゅù瀣珔闁搞劍绻堥弻娑㈠箻濡も偓鐎氼剟寮搁崒鐐粹拺闁圭瀛╃粈鈧梺绋匡功椤牐鐏嬪┑顔姐仜閸嬫捇鏌＄仦鍓ф创濠碉紕鍏橀、娑㈡倷閹碱厸鍋撳鍜佹富闁靛牆妫楁慨鍐磼椤旂晫鎳囩€?{','.join(error_file)}"

    print(compelete_str)



    return result




if __name__=="__main__":
    match_supported_suffix(file_suffix="pdf",supported_suffixes=office_suffixes)
    A=1
