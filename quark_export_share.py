import logging
#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
import asyncio
import json
from quark import QuarkUcSDK
import base64
import time
import asyncio
import os
import re

# 从分享URL中提取分享ID和密码
def extract_share_info_from_url(share_url):
    # 匹配分享URL格式，提取分享ID
    share_id_match = re.search(r'/s/([a-zA-Z0-9]+)', share_url)
    if not share_id_match:
        raise ValueError(f"无效的分享URL: {share_url}")
    share_id = share_id_match.group(1)
    # 提取密码（如果有）
    password_match = re.search(r'pwd=([a-zA-Z0-9]+)', share_url)
    password = password_match.group(1) if password_match else ""
    return share_id, password

def sanitize_string(s: str) -> str:
    """
    清理字符串中的无效Unicode字符，以避免编码错误。
    参数:
        s (str): 待处理的字符串。
    返回:
        str: 清理后的字符串。
    """
    return s.encode('utf-8', errors='replace').decode('utf-8')

def should_skip_quark_file(filename):
    env_filter = os.getenv("ENV_EXT_FILTER", "")
    if not env_filter:
        return False
    skip_exts = [ext.strip().lower() for ext in env_filter.split(',') if ext.strip()]
    if not filename:
        return False
    _, ext = os.path.splitext(filename)
    return ext.lower() in skip_exts

def export_share_info(share_url, cookie=""):
    json_data = {
            "usesBase62EtagsInExport": False,
            "files": [],
        }
    async def main(batch_size: int = 50):
        start_time = time.time()
        my_cookie = cookie  
        try:
            code, password = extract_share_info_from_url(share_url)
            logger.info(f"从URL提取到分享ID: {code}，密码: {password if password else '无'}")
        except ValueError as e:
            logger.error(f"错误: {e}")
            return
        
        async with QuarkUcSDK(cookie=my_cookie) as quark:
            # 1. 获取分享信息
            share_info_result = await quark.get_share_info(code, password)
            logger.info("--- 正在获取分享信息 --- ")
            
            if share_info_result.get("code") == 0:
                stoken = share_info_result["data"]["stoken"]
                
                # 2. 收集所有文件信息 (优先直接读取列表中的MD5)
                logger.info(f"--- 正在收集文件信息 --- ")
                
                # 待补充MD5的文件列表 (fid, token)
                files_needing_md5 = []
                # 待补充MD5的文件映射 {fid: file_base_obj}
                file_mapping = {}
                
                async for file_info in quark.get_share_file_list(
                    code=code,
                    passcode=password,
                    stoken=stoken,
                    dir_id=0,
                    is_get_folder=False,
                    is_recursion=True,
                ):
                    # 基础信息
                    # 提前获取 path/filename 并检查
                    clean_path = sanitize_string(file_info["RootPath"].lstrip('/'))
                    if should_skip_quark_file(clean_path):
                        logger.info(f"🚫 [Quark] 过滤文件: {clean_path}")
                        continue
                    
                    file_base = {
                        "size": file_info["size"],
                        "path": clean_path,
                    }
                    
                    # === 核心修复逻辑开始 ===
                    # 优先检查文件列表原始数据中是否已有有效的 MD5
                    origin_md5 = file_info.get("md5")
                    
                    if origin_md5 and isinstance(origin_md5, str) and len(origin_md5) == 32:
                        # 如果已有32位标准MD5，直接使用
                        file_base["etag"] = origin_md5.lower()
                        json_data["files"].append(file_base)
                    else:
                        # 只有当列表中没有MD5时，才加入“待获取队列”
                        file_mapping[file_info["fid"]] = file_base
                        files_needing_md5.append((file_info["fid"], file_info["share_fid_token"]))
                    # === 核心修复逻辑结束 ===
                    
                total_needing = len(files_needing_md5)
                total_found = len(json_data["files"])
                logger.info(f"--- 初步扫描: {total_found} 个文件已获取MD5，{total_needing} 个文件需进一步请求 --- ")
                
                # 3. 仅对缺失MD5的文件批量获取
                if total_needing > 0:
                    logger.info(f"--- 开始批量获取剩余文件的MD5 (批次大小: {batch_size}) --- ")
                    md5_results = await quark.batch_send_create_share_download_request(
                        code=code,
                        pwd=password,
                        stoken=stoken,
                        file_info_list=files_needing_md5,
                        batch_size=batch_size
                    )
                    
                    # 4. 处理补充结果
                    for fid, file_base in file_mapping.items():
                        # [重点修复] 增加 .get('md5') 的非空判断
                        # 只有当 md5 存在且不为空字符串时才处理
                        if fid in md5_results and md5_results[fid].get('md5'):
                            md5_info = md5_results[fid]
                            raw_md5 = md5_info['md5']
                            final_md5 = ""
                            
                            try:
                                # 处理可能的 Base64 编码
                                if '==' in raw_md5:    
                                    final_md5 = base64.b64decode(raw_md5).hex()
                                else:    
                                    final_md5 = raw_md5
                            except Exception: 
                                final_md5 = raw_md5 
                                
                            file_base["etag"] = final_md5
                            json_data["files"].append(file_base)
                        else:
                            # 如果 MD5 为空，记录明确的警告日志
                            # 这些文件如果不包含 etag，秒传肯定会失败，但为了保持文件列表完整性，
                            # 我们可以选择跳过或者依然加入（但 etag 为空）。
                            # 这里选择依然加入，但打出警告，方便用户在日志中看到哪些文件出问题了。
                            logger.warning(f"⚠️ 文件无法获取MD5 (API返回空值): {file_base['path']}")
                            file_base["etag"] = "" 
                            json_data["files"].append(file_base)
                            
                logger.info(f"--- 信息收集完成，共 {len(json_data['files'])} 个文件 ---")

            else:
                logger.error(f"--- 获取分享信息失败，错误码: {share_info_result.get('code')} --- ")
        
        end_time = time.time()
        execution_time = end_time - start_time
        logger.info(f"总耗时: {execution_time:.2f} 秒")
        
    asyncio.run(main())
    return json_data

# 如果直接运行此脚本
if __name__ == "__main__":
    share_url = "https://pan.quark.cn/s/c094a3711bcc"
    cookie = "_qk_bx_ck_v1=eyJkZXZpY2VJZCI6ImVDeSNBQU9vVDZndkhOTzBOL1paNlBEcjEvL1B6cWxTRU5tSXBCRkVKakthaEczdmVDMzhQbVNXaVNlUy9wTXFtSnAyUFU0PSIsImRldmljZUZpbmdlcnByaW50IjoiYzE0NWNkY2U1ZTYyODUwMWU3NDMwYTAyZTY3YjhiNGYifQ==; b-user-id=9929ebb9-0dc2-5ed5-abac-6b84b193e8c9; __wpkreporterwid_=d13832ca-d23d-4242-96fc-5d4cc2fe78b7; _UP_A4A_11_=wb9cb17952bb43fe8b4f3695eff09cdd; b-user-id=9929ebb9-0dc2-5ed5-abac-6b84b193e8c9; ctoken=MF6J0Q81r_ijS0nNy6i8Rxdr; web-grey-id=80f1ce41-95a4-4e83-a08b-df6951fbac96; web-grey-id.sig=EIB_ia64X9AP-Nyd-aSj3klEUTZRz8B_sR_JC3G_8YY; grey-id=e04a0571-d3fd-b80e-2759-bce0e9ca17d9; grey-id.sig=M9UApHT-QexIN6gtYxrXESFrsEMUmgfUTu7CUGukAlc; isQuark=true; isQuark.sig=hUgqObykqFom5Y09bll94T1sS9abT1X-4Df_lzgl8nM; __sdid=AAR18dAzGeXddKEvUjPHmcnz76Iom8plLlP7zUd/BcOr0W5CEDTkwiy0ocATiu28duDuKHQXVnPkAGXKvZHnXydoIwOL48AMq7fK99aF46dx1w==; _UP_D_=pc; xlly_s=1; __chkey=; __pus=1a099988568d7709539e6d50835cbdd7AASEqIoKZbQx6jCw49mNdNcecnB54KyEXwt24Ow1+NN5ytPT051rMt4Q95RottoHhdZeLZkN9keENVjrHQY5QOSM; __kp=9e9940c0-949f-11f0-bd58-8bb2451e1ac6; __kps=AARQDre99j4kWTEzt1WZFgeD; __ktd=eaaqz0znDaxM+jVdRG78ug==; __uid=AARQDre99j4kWTEzt1WZFgeD; isg=BM_PCpzo-wqyf__Fe3VclXrRXmPZ9CMW4vdHhOHZTD5FsOSy6MQUZtvjsuAOyPuO; tfstk=g46jDU2l2q0jeg1pGdrrNmYrfhps5uyeHctOxGHqXKpAXd_Wznl4iCz1NNQykxJvMhMRWNgxMrcxCd_eSEpNgE-OPGQYmhrcC_D1xGX4mdrDnivMByzULRscmdVeisFYYLhJvgK9X7RA-NgneyzUL8Px2dab8N73hqAJjUKvXCKYVQKMDdK9DdEWyhtn6qQ9BuZWbHH9XIKO2LK2XdLOWdE52Ux6BnQ9BusJrhhky4taGhIbv7MyRKoucaYSBABW2PYCc-k2A9-XGeIAhADgvnOXJiL7kX0hBQIM1OmiCCsAtNxdkqUeiGC5Pn9_UjpdV1QBqteEl3fV2M8RADHdvKt69ddSXAIWnUARepeKP3fR01BclcM9mtWe1eA7XAAwe9Rd9ZigxgpvXNAhQyDDkGIhK6JQUjpdV1QC1g7ZLe9TvfiWtAtW8uZSsfX3B_avmz-A7IKkDJr7V4GMM3xW8uZSsfAvqnCUVugSs; __puus=df4074814eb2c70631be5ab02bf2a9b2AARpAPoHtOuWl5F5UVWt6nL6nBUMVsUw9Go4NYcEZJzE2P7xx4JeJ8YhweFptj29Cex6g+vTIBJCrp2XYticR3b3104076oq1M3YOiydk9hZBntbeSUiT2Fu1Hu85i6FspPc0VnxlH6i5Cu9sU3F1axhMq03GAtGs/nKQiKVIfw/H+HGkiHMqCERIbKJxSf6dFYM1tzjzfx3VK5A/freUU3W"
    print(export_share_info(share_url, cookie))
