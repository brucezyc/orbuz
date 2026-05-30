"""
WorkspaceManager — _workspace/ Directory Management
====================================================
Reads/writes manifest.json, status.json, context.json, phases/*, log.json

Fully consistent with project-root docs/30-runtime-protocol.md.
"""

import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from orbuz.schema.workspace import RunStatus, RunManifest


class WorkspaceManager:
    """Manage the _workspace directory for a single workflow run"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base = Path(base_dir) if base_dir else Path.cwd() / "_workspace"
        self.base.mkdir(parents=True, exist_ok=True)

    # ── Initialization ──

    def init_run(self, plan: dict) -> str:
        """Create a new run workspace, returns run_id"""
        run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{plan['workflow']['name']}"
        run_dir = self.base / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "phases").mkdir()
        (run_dir / "bus").mkdir()
        (run_dir / "deliver").mkdir()

        # Write manifest
        manifest = RunManifest(
            run_id=run_id,
            workflow=plan["workflow"],
            parameters=plan.get("recon_summary", {}),
            stages=plan["plan"]["stages"],
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2)
        )

        # Write initial status
        stages_status = [
            {"id": s["id"], "status": "pending", "agents": []}
            for s in plan["plan"]["stages"]
        ]
        status = RunStatus(
            run_id=run_id,
            state="created",
            stages=stages_status,
        )
        (run_dir / "status.json").write_text(
            json.dumps(status.model_dump(), ensure_ascii=False, indent=2)
        )

        # Write initial context
        ctx = {"run_id": run_id, "resolved_paths": {}, "continuation": {}}
        (run_dir / "context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2)
        )

        # Update current symlink
        current_link = self.base / "current"
        if current_link.exists() or current_link.is_symlink():
            current_link.unlink()
        current_link.symlink_to(run_id)

        return run_id

    # ── State read/write ──

    def set_state(self, run_id: str, state: str):
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            data["state"] = state
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def set_current_stage(self, run_id: str, stage_idx: int):
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            data["current_stage_index"] = stage_idx
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def set_stage_completed(self, run_id: str, stage_id: str):
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    s["status"] = "completed"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_stage_status(self, run_id: str, stage_id: str) -> str:
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    return s.get("status", "unknown")
        return "unknown"

    def get_stage_summary(self, run_id: str, stage_id: str) -> dict:
        """Return stage summary (for checkpoint display)"""
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    return s.get("summary", {})
        return {}

    def set_continuation(self, run_id: str, note: str):
        path = self.base / run_id / "context.json"
        if path.exists():
            data = json.loads(path.read_text())
            data.setdefault("continuation", {})["note"] = note
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def reset_stage(self, run_id: str, stage_id: str):
        path = self.base / run_id / "status.json"
        if path.exists():
            data = json.loads(path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    s["status"] = "pending"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # ── Output read/write ──

    def write_output(self, run_id: str, stage_id: str, role: str, content: str):
        """Write agent output to phases/{stage_id}/{role}.md"""
        phase_dir = self.base / run_id / "phases" / stage_id
        phase_dir.mkdir(parents=True, exist_ok=True)
        (phase_dir / f"{role}.md").write_text(content)

    def read_output(self, run_id: str, stage_id: str, role: str) -> str:
        path = self.base / run_id / "phases" / stage_id / f"{role}.md"
        if path.exists():
            return path.read_text()
        return ""

    # ── Shared space ──

    def write_shared(self, run_id: str, filename: str, content: str):
        """Write a file to the shared/ directory"""
        shared_dir = self.base / run_id / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / filename).write_text(content)

    def read_shared(self, run_id: str, filename: str) -> str | None:
        """Read a file from the shared/ directory"""
        path = self.base / run_id / "shared" / filename
        if path.exists():
            return path.read_text()
        return None

    def list_shared(self, run_id: str) -> list[str]:
        """List all files in the shared/ directory"""
        shared_dir = self.base / run_id / "shared"
        if shared_dir.exists():
            return sorted(f.name for f in shared_dir.iterdir() if f.is_file())
        return []

    def inject_shared_context(self, run_id: str) -> str:
        """Build a shared/ directory listing for injection into agent context"""
        files = self.list_shared(run_id)
        if not files:
            return ""
        return "\nShared space files:\n" + "\n".join(f"  - {f}" for f in files)

    # ── Status query ──

    def read_current_status(self) -> dict | None:
        run_id = self.read_current_run_id()
        if not run_id:
            return None
        path = self.base / run_id / "status.json"
        if path.exists():
            return json.loads(path.read_text())
        return None

    def read_current_run_id(self) -> str | None:
        current = self.base / "current"
        if current.exists() and current.is_symlink():
            return current.resolve().name
        return None


if __name__ == "__main__":
    wm = WorkspaceManager()
    plan = {
        "workflow": {"name": "test-run"},
        "recon_summary": {},
        "plan": {
            "stages": [
                {"id": "01_search", "name": "Search", "agents": []},
                {"id": "02_report", "name": "Report", "agents": []},
            ]
        }
    }
    run_id = wm.init_run(plan)
    print(f"✅ Workspace created: {run_id}")
    print(f"  current → {wm.read_current_run_id()}")
