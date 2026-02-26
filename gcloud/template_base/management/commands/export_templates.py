"""
从数据库导出流程模板到 CSV 文件，每行一个流程

使用方法:
    # 导出全量流程
    python manage.py export_templates

    # 导出指定业务下的流程
    python manage.py export_templates --biz-id 100001

    # 指定输出目录
    python manage.py export_templates --biz-id 100001 --output-dir .
"""

import csv
import logging
import os

import ujson as json
from django.core.management.base import BaseCommand

from gcloud.core.models import Project
from gcloud.exceptions import FlowExportError
from gcloud.tasktmpl3.models import TaskTemplate
from gcloud.utils.dates import time_now_str

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "从数据库导出流程模板到 CSV 文件"

    def add_arguments(self, parser):
        parser.add_argument(
            "--biz-id",
            type=int,
            default=None,
            help="业务 ID (bk_biz_id)，不指定则导出全量",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default="./exported",
            help="输出目录，默认为当根目录的 exported 目录",
        )

    def handle(self, *args, **options):
        biz_id = options.get("biz_id")
        output_dir = options["output_dir"]

        # 查找项目
        project_qs = Project.objects.filter(is_disable=False)
        if biz_id is not None:
            project_qs = project_qs.filter(bk_biz_id=biz_id)

        projects = list(project_qs.values("id", "bk_biz_id", "name"))
        if not projects:
            self.stderr.write(self.style.ERROR("未找到符合条件的项目"))
            return

        # 构建 project_id -> (biz_id, biz_name) 映射
        project_map = {p["id"]: (p["bk_biz_id"], p["name"]) for p in projects}
        project_ids = list(project_map.keys())

        self.stdout.write(f"共找到 {len(project_ids)} 个项目")

        # 查询所有待导出的流程
        templates = list(
            TaskTemplate.objects.filter(
                project_id__in=project_ids,
                is_deleted=False,
            )
            .select_related("pipeline_template")
            .only("id", "project_id", "pipeline_template")
        )

        if not templates:
            self.stderr.write(self.style.WARNING("没有可导出的流程"))
            return

        self.stdout.write(f"待导出流程数: {len(templates)}")

        # 准备输出文件
        os.makedirs(output_dir, exist_ok=True)
        suffix = str(biz_id) if biz_id else "all"
        filename = "bk_sops_templates_%s_%s.csv" % (suffix, time_now_str())
        filepath = os.path.join(output_dir, filename)

        total_exported = 0
        failed_templates = []

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "业务ID", "业务名称", "流程ID", "流程名称", "流程JSON"])

            for idx, tmpl in enumerate(templates, 1):
                biz_id_val, biz_name = project_map.get(tmpl.project_id, ("", ""))
                tmpl_name = tmpl.pipeline_template.name if tmpl.pipeline_template else ""

                if idx % 50 == 0:
                    self.stdout.write(f"  进度: {idx}/{len(templates)}")

                try:
                    export_data = TaskTemplate.objects.export_templates(
                        [tmpl.id], is_full=False, project_id=tmpl.project_id
                    )
                    flow_json = json.dumps(export_data, sort_keys=True, ensure_ascii=False)
                except (FlowExportError, Exception) as e:
                    failed_templates.append((tmpl.id, tmpl_name, str(e)))
                    self.stderr.write(self.style.WARNING(f"  跳过流程 ID={tmpl.id} ({tmpl_name}): {e}"))
                    continue

                total_exported += 1
                writer.writerow([total_exported, biz_id_val, biz_name, tmpl.id, tmpl_name, flow_json])

        if total_exported == 0:
            self.stderr.write(self.style.WARNING("没有成功导出的流程"))
            os.remove(filepath)
            return

        self.stdout.write(self.style.SUCCESS(f"导出完成，共 {total_exported} 个流程: {filepath}"))
        if failed_templates:
            self.stderr.write(self.style.WARNING(f"以下 {len(failed_templates)} 个流程导出失败:"))
            for tid, name, err in failed_templates:
                self.stderr.write(f"  流程 ID={tid} ({name}): {err}")
