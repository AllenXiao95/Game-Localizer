from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from localizer.application.scan import ResourceScanner
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import ProjectRunner
from localizer.adapters.storage.glossary import GlossaryRepository
from localizer.adapters.storage.sqlite_tm import (
    AuthoritySwitchRefused,
    SQLiteTranslationMemory,
)
from localizer.migrations.legacy_tm import LegacyTMExporter, LegacyTMSynchronizer
from localizer.migrations.accepted_artifact import (
    AcceptedArtifactAdopter,
    AcceptedArtifactVerifier,
    ArtifactAdoptionRefused,
)
from localizer.rules.loader import load_validation_rule
from localizer.application.artifact import ReleaseBundle
from localizer.adapters.publishers.local import LocalPublisher
from localizer.compat.legacy import (
    LegacyAccessPolicy,
    LegacyMainFacade,
    LegacyPhase,
    phase_for_tm,
    resolve_phase,
)
from localizer.config import ConfigLoadError, load_project_config
from localizer.infrastructure.workspace import RunWorkspace, validate_run_id
from localizer.infrastructure.atomic_io import AtomicIO


app = typer.Typer(no_args_is_help=True, help="Game localization framework")


@app.command("validate-config")
def validate_config(config: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    try:
        loaded = load_project_config(config)
    except ConfigLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"valid: {loaded.project.id} ({loaded.languages.source} -> {loaded.languages.target})")


@app.command()
def scan(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    variant: Optional[str] = typer.Option(None, "--variant"),
) -> None:
    loaded = load_project_config(config)
    try:
        loaded = loaded.for_variant(variant)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    adapter = loaded.resources.adapters[0]
    result = ResourceScanner().scan(
        loaded.paths.source,
        includes=adapter.include,
        excludes=adapter.exclude,
    )
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


@app.command("workspace-init")
def workspace_init(
    root: Path = typer.Argument(...), run_id: str = typer.Argument(...)
) -> None:
    workspace = RunWorkspace(root, run_id).create()
    typer.echo(str(workspace.path))


@app.command("build")
def build_project(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    mode: BuildMode = typer.Option("preview", "--mode"),
    run_id: str = typer.Option(..., "--run-id"),
    variant: Optional[str] = typer.Option(
        None, "--variant",
        help="多目录项目选哪个资源变体（正式服/测试服等）；单目录项目留空",
    ),
) -> None:
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    loaded = load_project_config(config)
    try:
        loaded = loaded.for_variant(variant)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    result = ProjectRunner(loaded).run(mode=mode, run_id=run_id)
    typer.echo(
        json.dumps(
            {
                "mode": result.build.mode.value,
                "version": loaded.project.game_version,
                "output": str(result.build.output_root),
                "qa_report": str(result.build.qa_json),
                "artifact": str(result.build.bundle.artifact)
                if result.build.bundle
                else None,
                "manifest": str(result.build.bundle.manifest)
                if result.build.bundle
                else None,
                "variant": loaded.active_variant or None,
                "artifact": str(result.build.bundle.artifact) if result.build.bundle else None,
                "units": result.extracted_units,
                "tm_hits": result.tm_hits,
                "machine_successes": result.machine_successes,
                "failed_units": result.failed_units,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("rebuild-from-run")
def rebuild_from_run(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    parent_run_id: str = typer.Option(..., "--parent-run-id"),
    run_id: str = typer.Option(..., "--run-id"),
    mode: BuildMode = typer.Option("preview", "--mode"),
    variant: Optional[str] = typer.Option(None, "--variant"),
    version: Optional[str] = typer.Option(
        None,
        "--version",
        help="子运行使用的新游戏版本；留空继承 project.yaml",
    ),
) -> None:
    """应用人工修复并基于父运行增量重建。

    `completed + QA failed` 不能按原 run_id resume（那条路只对执行状态 `failed`
    开放），而普通新运行**不会**复用父 checkpoint 里已成功但还没正式提交的机器
    译文 —— 2026-08-04 的运行提交 3 条人工修复后，新计划仍有 1,427 条待翻译，
    只少了 4 条，等于要重复付一整轮的钱。

    这条命令：父运行不可变，创建新子运行；逐条校验源文指纹之后复用父运行的成功
    译文，叠加当前正式人工 TM，只把仍未解决的坐标送回模型，再跑完整的
    QualityGate。全部失败都已人工修复时，Provider 请求数为 0。
    """
    from localizer.application.project_runner import IncompatibleParentRun

    try:
        validate_run_id(run_id)
        validate_run_id(parent_run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    loaded = load_project_config(config)
    try:
        loaded = loaded.for_variant(variant)
        if version is not None:
            loaded = loaded.for_game_version(version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        result = ProjectRunner(loaded).rebuild_from_run(
            parent_run_id, mode=mode, run_id=run_id
        )
    except IncompatibleParentRun as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "mode": result.build.mode.value,
                "output": str(result.build.output_root),
                "qa_report": str(result.build.qa_json),
                "rebuild": result.rebuild.as_dict() if result.rebuild else None,
                "units": result.extracted_units,
                "tm_hits": result.tm_hits,
                "machine_successes": result.machine_successes,
                "failed_units": result.failed_units,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("tm-sync-legacy")
def tm_sync_legacy(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    legacy_tm: Path = typer.Argument(..., exists=True, dir_okay=False),
    force: bool = typer.Option(False, "--force"),
) -> None:
    loaded = load_project_config(config)
    with SQLiteTranslationMemory(loaded.tm.database) as tm:
        report_path = loaded.paths.workspace / "reports" / "legacy-tm-migration.json"
        report = LegacyTMSynchronizer(
            tm,
            project_id=loaded.project.id,
            source_locale=loaded.languages.source,
            target_locale=loaded.languages.target,
            validation_rule=load_validation_rule(
                loaded.rules.file, source_locale=loaded.languages.source
            ),
            # 术语表参与入库分类：违反已定稿术语的历史译文判 quarantined，
            # 不被坐标回填命中，从而交给模型重译，而不是等构建期整包阻断。
            glossary_terms=GlossaryRepository(loaded.glossary.file).load(),
        ).sync(
            legacy_tm,
            force=force,
            report_path=report_path,
            activate_write_guard=True,
        )
    typer.echo(json.dumps(report.__dict__, ensure_ascii=False, indent=2))


@app.command("tm-export-legacy")
def tm_export_legacy(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    destination: Path = typer.Argument(..., help="导出目标；默认拒绝覆盖已存在的文件"),
    include_quarantined: bool = typer.Option(
        False,
        "--include-quarantined",
        help="连 quarantined/unknown 行一起导出。旧格式没有 classification 维度，"
        "这些行导回去会变成无标记的正常译文——只在需要逐行往返时用。",
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """把 SQLite TM 导回旧 `history_tm.json` 形状（回滚能力，设计 §12.10）。

    切换 TM 权威源是单向的（`tm-switch-authority` 有一整套前置条件且拒绝重复
    切换），这条命令是那个棘轮唯一的逃生口：导出的文件可以被旧入口直接读。

    默认**不导出** quarantined/unknown 行，也**不覆盖**已存在的目标——回滚的
    目标往往就是旧 TM 本身，用导出结果盖掉它正是要防的事故。
    """
    loaded = load_project_config(config)
    report_path = loaded.paths.workspace / "reports" / "legacy-tm-export.json"
    with SQLiteTranslationMemory(loaded.tm.database, read_only=True) as tm:
        try:
            report = LegacyTMExporter(tm, project_id=loaded.project.id).export(
                destination,
                include_quarantined=include_quarantined,
                overwrite=overwrite,
                report_path=report_path,
            )
        except FileExistsError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2)
    typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


@app.command("tm-switch-authority")
def tm_switch_authority(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    behavior_baseline: Path = typer.Option(
        ..., "--behavior-baseline", exists=True, dir_okay=False
    ),
    data_baseline: Path = typer.Option(
        ..., "--data-baseline", exists=True, dir_okay=False
    ),
    legacy_tm: Path = typer.Option(
        ...,
        "--legacy-tm",
        exists=True,
        dir_okay=False,
        help="旧 history_tm.json。必填：这是唯一验证「旧入口写入已冻结」的判据",
    ),
    expected_legacy_rows: Optional[int] = typer.Option(
        None,
        "--expected-legacy-rows",
        help="核对清楚之后显式声明你认可的存量行数；留空则要求等于同步记录的累加值",
    ),
) -> None:
    loaded = load_project_config(config)
    with SQLiteTranslationMemory(loaded.tm.database) as tm:
        try:
            evidence = tm.switch_authority(
                behavior_baseline,
                data_baseline,
                legacy_source=legacy_tm,
                project_id=loaded.project.id,
                expected_legacy_rows=expected_legacy_rows,
            )
        except AuthoritySwitchRefused as exc:
            typer.echo(f"拒绝切换 TM 权威源：{exc}", err=True)
            raise typer.Exit(code=2)
    typer.echo(json.dumps(evidence, ensure_ascii=False, indent=2))


@app.command("tm-adopt-artifact")
def tm_adopt_artifact(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    resources_root: Optional[Path] = typer.Option(
        None, "--resources-root", exists=True, file_okay=False
    ),
    variant: Optional[str] = typer.Option(None, "--variant"),
    apply: bool = typer.Option(
        False, "--apply", help="Apply the attested artifact to SQLite; default is dry-run."
    ),
    accepted_by: str = typer.Option(
        "", "--accepted-by", help="Human/project-owner attestation recorded in the baseline."
    ),
    backup: Optional[Path] = typer.Option(None, "--backup", dir_okay=False),
    report: Optional[Path] = typer.Option(None, "--report", dir_okay=False),
    allow_remote_override: bool = typer.Option(False, "--allow-remote-override"),
) -> None:
    loaded = load_project_config(config)
    try:
        loaded = loaded.for_variant(variant)
        with SQLiteTranslationMemory(loaded.tm.database) as tm:
            adopter = AcceptedArtifactAdopter(
                loaded, tm, manifest, resources_root=resources_root
            )
            if not apply:
                payload, _entries = adopter.analyze(accepted_by=accepted_by)
                if report is not None:
                    AtomicIO.write_json(report, payload)
            else:
                backup_path = backup or (
                    loaded.paths.workspace
                    / "backups"
                    / f"tm-before-artifact-{adopter.bundle.run_id}.sqlite3"
                )
                report_path = report or (
                    loaded.paths.workspace
                    / "reports"
                    / f"data-baseline-{adopter.bundle.run_id}.json"
                )
                payload = adopter.adopt(
                    accepted_by=accepted_by,
                    backup_path=backup_path,
                    report_path=report_path,
                    allow_remote_override=allow_remote_override,
                )
    except (ArtifactAdoptionRefused, TMGuardError, OSError, ValueError) as exc:
        typer.echo(f"refusing accepted-artifact adoption: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("tm-verify-artifact")
def tm_verify_artifact(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    run_id: str = typer.Option(..., "--run-id"),
    resources_root: Optional[Path] = typer.Option(
        None, "--resources-root", exists=True, file_okay=False
    ),
    variant: Optional[str] = typer.Option(None, "--variant"),
    report: Optional[Path] = typer.Option(None, "--report", dir_okay=False),
) -> None:
    loaded = load_project_config(config)
    try:
        loaded = loaded.for_variant(variant)
        report_path = report or (
            loaded.paths.workspace / "reports" / f"behavior-baseline-{run_id}.json"
        )
        payload = AcceptedArtifactVerifier(
            loaded, manifest, resources_root=resources_root
        ).verify(run_id=run_id, report_path=report_path)
    except (ArtifactAdoptionRefused, OSError, ValueError) as exc:
        typer.echo(f"accepted-artifact verification failed: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("publish-local")
def publish_local(
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    destination: Path = typer.Argument(...),
) -> None:
    bundle = ReleaseBundle.load(manifest)
    receipt = LocalPublisher(
        destination,
        # Public metadata has a fixed compatibility filename.  Keep versions
        # isolated even when this convenience command bypasses project.yaml.
        versioned_prefix=bundle.public_metadata is not None,
    ).publish(bundle)
    typer.echo(
        json.dumps(
            {
                "target": receipt.target,
                "objects": [item.__dict__ for item in receipt.objects],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("publish")
def publish_all(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    """把已通过 QualityGate 的正式制品发布到 config.publish.targets 的全部目标。

    单个目标失败不影响其余，也不触碰本地制品；结束时若有失败则以非零码退出，
    并打印可重试清单（[F24]）。
    """
    from localizer.application.publish import PublishOrchestrator

    loaded = load_project_config(config)
    if not loaded.publish.targets:
        raise typer.BadParameter("config.publish.targets is empty; nothing to publish")
    # 治理闸门：M0 凭据轮换未完成时，所有非 local 目标被拒（fail-closed）。
    results = PublishOrchestrator(security=loaded.security).publish(
        ReleaseBundle.load(manifest), loaded.publish
    )
    typer.echo(
        json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2)
    )
    failed = [r for r in results if not r.succeeded]
    if failed:
        retryable = [r.target for r in failed if r.retryable]
        typer.echo(
            f"{len(failed)}/{len(results)} target(s) failed; "
            f"local artifact kept at {ReleaseBundle.load(manifest).artifact}",
            err=True,
        )
        if retryable:
            typer.echo(f"retryable: {', '.join(retryable)}", err=True)
        raise typer.Exit(1)


@app.command("qa-accept-debt")
def qa_accept_debt(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    qa_report: Path = typer.Argument(..., exists=True, dir_okay=False),
    note: str = typer.Option("", "--note", help="为什么现在接受这批债"),
    allow_new_keys: bool = typer.Option(
        False,
        "--allow-new-keys",
        help="允许基线新增此前未登记的 debt_key（默认拒绝，防止棘轮被静默复位）",
    ),
) -> None:
    """把一次 QA 报告里的**存量债**登记进基线，之后只有新增的才会阻断 release。

    背景：QualityGate 原本对全量 error 零容忍。真机验证实测 853 个 error 里
    849 个来自历史 TM 命中、只有 2 个来自本次机器新译 —— 只要历史 TM 里还有
    一条坏账，任何 release 都发不出去，增量打包被存量债永久阻塞。

    这条命令是那个问题的显式出口，不是放行开关：
    - 本次运行自己产出的译文上的 error **不会**被登记，永远零容忍；
    - 基线是一把棘轮，债只能减不能增，新出现的存量缺陷照样阻断；
    - 基线文件进版本库，谁接受了哪些债可 review、可回溯。
    """
    from localizer.application.quality_gate import (
        LegacyDebtBaseline,
        QARecord,
        is_this_run,
    )

    loaded = load_project_config(config)
    baseline_path = loaded.quality_gate.legacy_debt_baseline
    if baseline_path is None:
        raise typer.BadParameter(
            "project config has no quality_gate.legacy_debt_baseline; "
            "add it first so the accepted debt is version-controlled"
        )
    payload = json.loads(qa_report.read_text(encoding="utf-8"))
    records = [
        QARecord(
            code=item.get("code", ""),
            severity=item.get("severity", ""),
            message=item.get("message", ""),
            stable_identity=item.get("stable_identity"),
            relative_path=item.get("relative_path"),
            details=item.get("details") or {},
            provenance=item.get("provenance", "unknown"),
        )
        for item in payload.get("issues", [])
    ]
    skipped = [r for r in records if r.severity == "error" and is_this_run(r.provenance)]

    # provenance 是分流依据。缺这个键的报告（provenance 落地之前产出的）会让
    # 本该零容忍的机器新译被记成存量债 —— 必须显式告知，不能静默接受。
    if any("provenance" not in item for item in payload.get("issues", [])):
        typer.echo(
            "警告：这份 QA 报告的 issue 没有 provenance 键，说明它产出于 provenance "
            "落地之前。所有记录会被当作存量债处理，其中可能混有本该零容忍的机器新译。"
            "建议重跑一次 preview 后再登记。",
            err=True,
        )

    # 棘轮的语义是「债只能减不能增」。这条命令是**全量重写**基线，如果不比对
    # 就写，任何新出现的坏账（包括人工在审查面板里亲手写出来的）都会被整批
    # 登记成「已接受存量债」，棘轮当场失效，事后只能翻 git。
    previous = LegacyDebtBaseline.load(baseline_path) if baseline_path.is_file() else None
    if previous is not None:
        pending = {
            r.debt_key for r in records
            if r.severity == "error" and not is_this_run(r.provenance)
        }
        new_keys = sorted(pending - previous.accepted)
        if new_keys and not allow_new_keys:
            typer.echo(
                f"拒绝写入：这次会新增 {len(new_keys)} 个此前未登记的 debt_key。\n"
                f"基线是一把棘轮，债只能减不能增。逐条确认这些确实是**存量**缺陷、"
                f"而不是本轮新写出来的，再加 --allow-new-keys 重跑。",
                err=True,
            )
            for key in new_keys[:50]:
                typer.echo(f"  + {key}", err=True)
            if len(new_keys) > 50:
                typer.echo(f"  …… 另有 {len(new_keys) - 50} 个", err=True)
            raise typer.Exit(2)

    written = LegacyDebtBaseline().write(baseline_path, records, note=note)
    accepted = json.loads(written.read_text(encoding="utf-8"))
    typer.echo(
        json.dumps(
            {
                "baseline": str(written),
                "accepted_total": accepted["accepted_total"],
                "by_code": accepted["by_code"],
                "refused_this_run_errors": len(skipped),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if skipped:
        typer.echo(
            f"注意：{len(skipped)} 条 error 出在本次运行自己产出的译文上，"
            f"**没有**被登记 —— 这些必须真修，不能记账。",
            err=True,
        )


@app.command("dashboard")
def dashboard(
    config: Path = typer.Argument(..., exists=True, dir_okay=False),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    variant: Optional[str] = typer.Option(
        None,
        "--variant",
        help="多目录项目要看的资源目录（正式服/测试服）；不传时用 paths.default_variant",
    ),
) -> None:
    """启动观测面板和受控本地任务入口。

    面板提供 QA 缺陷的单人定点修复（framework-design.md §16.4）：编辑落本地 TM
    并留完整审计。它不组织人工审核流程 —— 没有审核队列、任务分发、多人审批，
    也不改 ParaTranz stage。批量人工翻译与校对统一在 ParaTranz 完成。
    任务启动只在回环地址启用；改 --host 对外展示时会自动关闭写接口。

    多目录项目一次只看一个变体：run 落在 `<workspace>/<variant>/` 下，
    不投影的话面板会正常打开然后显示「没有运行」。
    """
    from localizer.web import DashboardServer
    from localizer.web.server import (
        DashboardBindError,
        VariantRequired,
        build_collector,
    )

    try:
        collector = build_collector(config, Path.cwd(), variant=variant)
    except VariantRequired as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        server = DashboardServer(collector, host=host, port=port)
    except DashboardBindError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"localizer dashboard: {server.url}")
    typer.echo("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        server.stop()


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def legacy(
    context: typer.Context,
    repository_root: Path = typer.Option(Path.cwd(), "--repository-root"),
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        dir_okay=False,
        help="项目配置。必填：阶段由 TM 的实际状态派生，不由调用方自己声明",
    ),
    phase: Optional[LegacyPhase] = typer.Option(
        None,
        "--phase",
        help="显式收紧阶段。只允许比派生结果更严，不允许更松。",
    ),
) -> None:
    """在兼容门面下调用旧入口。

    阶段原本是一个**默认 M1_M2**（最宽松）的命令行开关，等于让调用方自己声明
    该受什么约束 —— 切完 TM 权威源之后照样能 `--save-tm` 整库覆盖
    `history_tm.json`。

    `--config` 因此是**必填**的：它曾经可选，不传就回落到 M1_M2，于是刚堵上的
    洞又留了一个「不写这个参数就当没切过」的口子。「不知道库处于哪个阶段」和
    「库处于最宽松阶段」是两件完全不同的事，前者只能 fail-closed。
    `--phase` 退化成只能收紧的覆写。
    """
    loaded = load_project_config(config)
    with SQLiteTranslationMemory(loaded.tm.database, read_only=True) as tm:
        derived = phase_for_tm(tm)
    try:
        effective = resolve_phase(derived, phase)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    facade = LegacyMainFacade(repository_root, LegacyAccessPolicy(effective))
    raise typer.Exit(facade.run(context.args))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
