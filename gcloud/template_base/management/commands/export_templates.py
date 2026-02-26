"""
从数据库导出指定业务下的全量项目流程模板到 .dat 文件

使用方法:
    # 导出指定业务下的全量项目流程
    python manage.py export_templates --biz-id 100001

    # 指定输出目录
    python manage.py export_templates --biz-id 100001 --output-dir /tmp
"""

import base64
import hashlib
import logging
import os

import ujson as json
from django.conf import settings
from django.core.management.base import BaseCommand

from gcloud.core.models import Project
from gcloud.exceptions import FlowExportError
from gcloud.tasktmpl3.models import TaskTemplate
from gcloud.utils.dates import time_now_str

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "从数据库导出指定业务下的全量项目流程模板到 .dat 文件"

    def add_arguments(self, parser):
        parser.add_argument(
            "--biz-id",
            type=int,
            required=True,
            help="业务 ID (bk_biz_id)",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default="./exported",
            help="输出目录，默认为当根目录的 exported 目录",
        )

    def handle(self, *args, **options):
        biz_id = options["biz_id"]
        output_dir = options["output_dir"]

        projects = Project.objects.filter(bk_biz_id=biz_id, is_disable=False)
        if not projects.exists():
            self.stderr.write(self.style.ERROR(f"未找到业务 ID={biz_id} 对应的有效项目"))
            return

        project_ids = list(projects.values_list("id", flat=True))
        self.stdout.write(f"业务 ID={biz_id}，关联项目数: {len(project_ids)}")

        batch_size = 300
        total_exported = 0
        failed_templates = []

        # 合并后的完整数据
        merged_data = None

        for project_id in project_ids:
            template_id_list = list(
                TaskTemplate.objects.filter(
                    project_id=project_id,
                    is_deleted=False,
                ).values_list("id", flat=True)
            )

            if not template_id_list:
                self.stdout.write(f"项目 ID={project_id} 下没有可导出的流程，跳过")
                continue

            self.stdout.write(f"项目 ID={project_id}，待导出流程数: {len(template_id_list)}")

            # 每 300 个流程分批调用 export_templates，结果合并到 merged_data
            for start in range(0, len(template_id_list), batch_size):
                batch_ids = template_id_list[start : start + batch_size]
                self.stdout.write(f"  正在导出 {len(batch_ids)} 个流程 (offset={start})")

                try:
                    batch_data = TaskTemplate.objects.export_templates(batch_ids, is_full=False, project_id=project_id)
                except (FlowExportError, Exception) as e:
                    self.stderr.write(self.style.WARNING(f"  批量导出失败 (offset={start}): {e}，回退到逐个导出"))
                    for tid in batch_ids:
                        try:
                            single_data = TaskTemplate.objects.export_templates(
                                [tid], is_full=False, project_id=project_id
                            )
                        except (FlowExportError, Exception) as e2:
                            failed_templates.append((tid, str(e2)))
                            self.stderr.write(self.style.WARNING(f"  跳过流程 ID={tid}: {e2}"))
                            continue
                        merged_data = self._merge(merged_data, single_data)
                        total_exported += 1
                    continue

                merged_data = self._merge(merged_data, batch_data)
                total_exported += len(batch_ids)

        if merged_data is None or total_exported == 0:
            self.stderr.write(self.style.WARNING(f"业务 ID={biz_id} 下没有可导出的项目流程"))
            return

        templates_data = json.loads(json.dumps(merged_data, sort_keys=True))

        data_string = (json.dumps(templates_data, sort_keys=True) + settings.TEMPLATE_DATA_SALT).encode("utf-8")
        digest = hashlib.md5(data_string).hexdigest()

        file_data = base64.b64encode(
            json.dumps({"template_data": templates_data, "digest": digest}, sort_keys=True).encode("utf-8")
        )

        os.makedirs(output_dir, exist_ok=True)
        filename = "bk_sops_%s_%s.dat" % (biz_id, time_now_str())
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "wb") as f:
            f.write(file_data)

        self.stdout.write(self.style.SUCCESS(f"导出完成，共 {total_exported} 个流程: {filepath}"))
        if failed_templates:
            self.stderr.write(self.style.WARNING(f"以下 {len(failed_templates)} 个流程导出失败:"))
            for tid, err in failed_templates:
                self.stderr.write(f"  流程 ID={tid}: {err}")

    @staticmethod
    def _merge(merged, batch):
        """将一批 export_templates 的结果合并到 merged 中"""
        if merged is None:
            return batch

        merged["template"].update(batch["template"])
        merged["pipeline_template_data"]["template"].update(batch["pipeline_template_data"]["template"])

        for be_ref, ref_info in batch["pipeline_template_data"].get("refs", {}).items():
            if be_ref not in merged["pipeline_template_data"].setdefault("refs", {}):
                merged["pipeline_template_data"]["refs"][be_ref] = ref_info
            else:
                for tmp_key, nodes in ref_info.items():
                    if tmp_key not in merged["pipeline_template_data"]["refs"][be_ref]:
                        merged["pipeline_template_data"]["refs"][be_ref][tmp_key] = nodes
                    else:
                        existing = merged["pipeline_template_data"]["refs"][be_ref][tmp_key]
                        if isinstance(existing, list) and isinstance(nodes, list):
                            seen = set(existing)
                            existing.extend(n for n in nodes if n not in seen)
                        elif isinstance(existing, dict) and isinstance(nodes, dict):
                            existing.update(nodes)

        return merged
