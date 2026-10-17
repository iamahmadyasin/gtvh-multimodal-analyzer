"""Command-line interface of GTVH Multimodal Analyzer.

Each subcommand acts on a project folder; run ``gtvh -h`` for the list.

Typical usage example:

  gtvh init my-study --name "My study"
  gtvh annotate --provider anthropic --batch
"""

import argparse
import getpass
import sys
import threading

from . import __version__
from .framework import STAGES, CODING_DIMENSIONS
from .project import Project, ProjectError
from .providers import PROVIDERS, ProviderError, delete_key, get_key, load_env_file, make_provider, set_key

USAGE = """Command line: gtvh <command> [options]. Run `gtvh <command> -h` for details.

    gtvh init PROJECT --name NAME             create a project folder
    gtvh import PATH...                        copy images into the project's images folder
    gtvh metadata                              optional: fill metadata.csv from the image file names
    gtvh annotate --provider anthropic         annotate (Stages 1-3; add --stage 4 for corpus coding)
    gtvh validate [--run RUN]                  re-check outputs against schemas and rules
    gtvh export --run RUN                      Excel workbook with all annotations and a review queue
    gtvh compare --runs A B [C ...]            agreement between runs (use 'adjudicated' for your gold)
    gtvh review sample|summary --run RUN       draw a random review sample; review progress and accuracy
    gtvh codebook draft|induce|check|accept    build the Stage 4 codebook
    gtvh keys set|delete|status [PROVIDER]     manage API keys (stored in the OS credential store)
    gtvh models PROVIDER                       list the models an API key can use

Most commands act on the project in the current folder; use -p PATH for another one.
"""


def _project(args):
    return Project(args.project)


def cmd_init(args):
    p = Project.create(args.path, args.name or args.path.rstrip("/\\").split("/")[-1].split("\\")[-1],
                       args.description or "")
    print(f"Created project '{p.name}' in {p.root}\nNext: put the cartoons in {p.images_dir} (or: gtvh import <images> -p \"{p.root}\")")


def cmd_import(args):
    p = _project(args)
    print(f"Copied {len(p.import_images(args.paths))} image(s) into {p.images_dir}.")


def cmd_metadata(args):
    p = _project(args)
    parsed, total = p.build_metadata()
    print(f"Wrote {p.metadata_path.name}: {parsed} of {total} file names matched DATE_CARTOONIST_PUBLICATION "
          "(e.g. 2024-03-15_paresh-nath_cagle.png); existing values were kept.")


def _parse_stages(values):
    if not values or values == ["all"]:
        return (1, 2, 3)
    stages = sorted({int(x) for x in values if x != "all"} | ({1, 2, 3} if "all" in values else set()))
    bad = [s for s in stages if s not in STAGES]
    if bad:
        raise SystemExit(f"Unknown stage(s): {bad}")
    return tuple(stages)


def cmd_annotate(args):
    from .pipeline import RunConfig, Runner, dry_run
    p = _project(args)
    d = p.defaults()
    stages = _parse_stages(args.stage)
    if args.dry_run:
        out = dry_run(p, stages)
        print(f"Wrote the assembled prompts, inputs and schemas to {out} (later stages use a calibration "
              "example as filler input).")
        return
    provider = args.provider or d.get("provider") or "anthropic"
    same = provider == d.get("provider")
    model = args.model or (d.get("model") if same else None)
    effort = args.effort or (d.get("effort") if same else None)
    cfg = RunConfig(provider=provider, model=model, effort=effort,
                    run=args.run, stages=stages, ids=args.ids, max_items=args.max_items, force=args.force,
                    retries=args.retries, workers=args.workers or d.get("workers") or 4,
                    use_schema=not args.no_schema, timeout=args.timeout, batch=args.batch)
    cancel = threading.Event()
    runner = Runner(p, cfg, cancel=cancel)
    try:
        summary = runner.run()
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelled. Run the same command again to continue.")
        return 1
    print(f"Next: gtvh export --run {runner.run_name}")
    return 1 if summary.stopped and summary.stopped not in ("cancelled",) and "batch size" not in summary.stopped else 0


def cmd_validate(args):
    from .export import validate_run
    p = _project(args)
    runs = [args.run] if args.run else p.runs()
    n_err = n_warn = n = 0
    for run in runs:
        for cid, st, errs, warns in validate_run(p, run):
            n += 1
            for e in errs:
                print(f"ERROR   {run}/{cid} stage {st}: {e}")
            for w in warns:
                print(f"warning {run}/{cid} stage {st}: {w}")
            n_err += len(errs)
            n_warn += len(warns)
    print(f"\n{n} stage output(s) in {len(runs)} run(s) checked: {n_err} error(s), {n_warn} warning(s).")
    return 1 if n_err else 0


def cmd_export(args):
    from .export import export_run
    p = _project(args)
    runs = args.run or p.runs()
    if not runs:
        raise SystemExit("No runs yet.")
    for run in runs:
        path, n, n_review = export_run(p, run)
        print(f"{run}: {n} cartoon(s) -> {path} ({n_review} stage result(s) in review_queue)")


def cmd_compare(args):
    from .compare import compare_runs
    p = _project(args)
    only = None
    if args.sample:
        from .review import sample
        s = sample(p)
        if not s:
            raise SystemExit("No review sample yet: gtvh review sample --run RUN --n 100")
        only = s["ids"]
    path, ids, summary = compare_runs(p, args.runs, only_ids=only)
    print(f"{len(ids)} cartoon(s) in all of: {', '.join(args.runs)}{' (review sample only)' if only else ''}")
    print(f"{'KR':<15} {'field':<30} {'n':>4}  {'agree':>6}  {'alpha/jacc':>10}")
    for r in summary:
        extra = r.get("krippendorff_alpha") if r["kind"] == "category" else r.get("mean_pairwise_jaccard")
        print(f"{r['kr']:<15} {r['field']:<30} {r['n']:>4}  {r.get('all_runs_agree', ''):>6}  "
              f"{'' if extra is None else extra:>10}")
    print(f"\nWrote {path}")


def cmd_review(args):
    from . import review
    p = _project(args)
    if args.action == "sample":
        s = review.draw_sample(p, args.run, args.n, args.seed)
        print(f"Review sample: {s['n']} of {s['population']} cartoons in run '{args.run}' (seed {s['seed']}). "
              "Review them in the app (Review tab, show: review sample).")
        return 0
    s = review.sample(p)
    ids = s["ids"] if args.sample and s else None
    if args.sample and not s:
        raise SystemExit("No review sample yet: gtvh review sample --run RUN --n 100")
    print(f"Run '{args.run}'{' - review sample of ' + str(len(ids)) if ids else ''}")
    print(f"{'stage':<6} {'outputs':>8} {'reviewed':>9} {'accepted':>9} {'corrected':>10} {'accepted %':>11}")
    for r in review.summary(p, args.run, ids):
        share = "" if r["accepted_share"] is None else f"{100 * r['accepted_share']:.1f}"
        print(f"{r['stage']:<6} {r['outputs']:>8} {r['reviewed']:>9} {r['accepted']:>9} {r['corrected']:>10} {share:>11}")


def cmd_codebook(args):
    from . import codebook
    p = _project(args)
    if args.action == "check":
        target = p.draft_path if args.draft else p.codebook_path
        problems = p.codebook_problems(target)
        print("\n".join(problems) if problems else f"{target.name} is valid.")
        return 1 if problems else 0
    if args.action == "accept":
        path = codebook.accept_draft(p)
        print(f"Accepted the draft as {path.name}. Next: gtvh annotate --stage 4")
        return 0
    provider = make_provider(args.provider or p.defaults().get("provider") or "anthropic", model=args.model,
                             effort=args.effort)
    dims = args.dimension or list(CODING_DIMENSIONS)
    if args.action == "draft":
        path = codebook.draft(p, provider, dims, args.size)
    else:
        if not args.run:
            raise SystemExit("induce needs --run RUN (the run whose annotations to group)")
        path = codebook.induce(p, args.run, provider, dims, args.size)
    print(f"Wrote {path}. Review and edit it, then: gtvh codebook accept")


def cmd_keys(args):
    names = [args.provider] if args.provider else list(PROVIDERS)
    if args.action == "set":
        if not args.provider:
            raise SystemExit("Name the provider: gtvh keys set anthropic|openai|gemini")
        key = getpass.getpass(f"API key for {PROVIDERS[args.provider]['label']} (input hidden): ")
        if not key.strip():
            raise SystemExit("No key entered.")
        set_key(args.provider, key)
        print("Saved in the operating system's credential store.")
    elif args.action == "delete":
        for n in names:
            delete_key(n)
        print("Deleted.")
    else:
        for n in names:
            _, source = get_key(n)
            print(f"{n:<10} {source or 'no key'}")


def cmd_models(args):
    prov = make_provider(args.provider, model=PROVIDERS[args.provider]["default_model"] or "-")
    for m in prov.list_models():
        print(m)


def build_parser():
    ap = argparse.ArgumentParser(prog="gtvh", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"gtvh {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, project=True):
        sp = sub.add_parser(name, help=help_, description=help_)
        if project:
            sp.add_argument("-p", "--project", default=".", help="project folder (default: current folder)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init", cmd_init, "create a project folder", project=False)
    sp.add_argument("path")
    sp.add_argument("--name")
    sp.add_argument("--description")

    sp = add("import", cmd_import, "copy images into the project's images folder")
    sp.add_argument("paths", nargs="+", help="image files or folders")

    sp = add("annotate", cmd_annotate, "annotate cartoons with a model")
    sp.add_argument("--provider", choices=list(PROVIDERS))
    sp.add_argument("--model", help="model id (default: the project default or the provider's default)")
    sp.add_argument("--effort", help="reasoning effort, e.g. low|medium|high (provider-specific)")
    sp.add_argument("--stage", nargs="+", help="stages to run: 1 2 3 4 or all (= 1 2 3; default)")
    sp.add_argument("--ids", nargs="+", help="only these cartoon_ids")
    sp.add_argument("--max-items", type=int, help="stop after this many cartoons (batch size)")
    sp.add_argument("--run", help="run folder name (default: <provider>-<model>[-<effort>])")
    sp.add_argument("--force", action="store_true", help="redo stages that already have output")
    sp.add_argument("--retries", type=int, default=1, help="extra attempts after a validation failure")
    sp.add_argument("--workers", type=int, help="cartoons annotated in parallel (default 4)")
    sp.add_argument("--timeout", type=int, default=600, help="seconds per API call")
    sp.add_argument("--no-schema", action="store_true", help="do not send the structured-output schema")
    sp.add_argument("--dry-run", action="store_true", help="write the assembled prompts only; no model calls")
    sp.add_argument("--batch", action="store_true",
                    help="use the provider's batch API: about half the price, results within 24 hours")

    add("metadata", cmd_metadata, "optional: fill metadata.csv from the image file names")

    sp = add("validate", cmd_validate, "re-check outputs against the schemas and rules")
    sp.add_argument("--run")

    sp = add("export", cmd_export, "write an Excel workbook per run")
    sp.add_argument("--run", nargs="+")

    sp = add("compare", cmd_compare, "agreement between runs")
    sp.add_argument("--runs", nargs="+", required=True)
    sp.add_argument("--sample", action="store_true", help="only the cartoons in the review sample")

    sp = add("review", cmd_review, "review sample and review summary")
    sp.add_argument("action", choices=["sample", "summary"])
    sp.add_argument("--run", required=True)
    sp.add_argument("--n", type=int, default=100, help="sample: number of cartoons")
    sp.add_argument("--seed", type=int, help="sample: random seed (recorded; default random)")
    sp.add_argument("--sample", action="store_true", help="summary: only the review sample")

    sp = add("codebook", cmd_codebook, "build the Stage 4 codebook")
    sp.add_argument("action", choices=["draft", "induce", "check", "accept"])
    sp.add_argument("--run", help="induce: the run whose annotations to group")
    sp.add_argument("--dimension", nargs="+", choices=list(CODING_DIMENSIONS))
    sp.add_argument("--provider", choices=list(PROVIDERS))
    sp.add_argument("--model")
    sp.add_argument("--effort")
    sp.add_argument("--size", default="10-20", help="approximate number of categories, e.g. 8-12")
    sp.add_argument("--draft", action="store_true", help="check: check codebook.draft.yaml")

    sp = add("keys", cmd_keys, "manage API keys", project=False)
    sp.add_argument("action", choices=["set", "delete", "status"])
    sp.add_argument("provider", nargs="?", choices=list(PROVIDERS))

    sp = add("models", cmd_models, "list available models", project=False)
    sp.add_argument("provider", choices=list(PROVIDERS))

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    load_env_file(getattr(args, "project", None))
    try:
        return args.fn(args) or 0
    except (ProjectError, ProviderError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

