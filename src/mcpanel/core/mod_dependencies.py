"""Resolve the required Modrinth dependency graph for a profile."""

import httpx


MAX_DEPENDENCIES = 64
MAX_DEPENDENCY_DEPTH = 12


async def resolve_required_dependencies(provider, root_version, mc_version, loader,
                                        installed_projects, reserved_projects, local_hashes,
                                        installed_versions=None):
    """Return unique compatible dependency versions and display metadata."""
    resolved = []
    projects = []
    visited = set(installed_projects) | set(reserved_projects)
    active = set()
    required_versions = {}
    installed_versions = installed_versions or {}

    async def visit(version, depth):
        if depth > MAX_DEPENDENCY_DEPTH:
            raise ValueError("Dependency chain exceeds the safe depth limit.")
        for dependency in version.get("dependencies", []):
            if dependency.get("dependency_type") != "required":
                continue
            project_id = dependency.get("project_id")
            version_id = dependency.get("version_id")
            if not project_id and not version_id:
                continue
            if project_id and version_id:
                previous_version = required_versions.setdefault(project_id, version_id)
                if previous_version != version_id:
                    raise ValueError(f"Dependency {project_id} is required at two different versions.")
            key = project_id or version_id
            if key in visited:
                if project_id and version_id and project_id in installed_versions and installed_versions[project_id] != version_id:
                    raise ValueError(f"Dependency {project_id} requires a different version than the one already installed.")
                continue
            if key in active:
                raise ValueError("A circular required dependency was found.")
            if len(visited) >= MAX_DEPENDENCIES:
                raise ValueError("This mod requires too many dependencies to install safely.")
            active.add(key)
            try:
                if version_id:
                    child = await provider.version(version_id)
                elif project_id:
                    candidates = await provider.versions(project_id, mc_version, loader)
                    if not candidates:
                        raise ValueError(f"No compatible version is available for {project_id}.")
                    child = candidates[0]
                else:
                    continue
                if mc_version not in child.get("game_versions", []) or loader not in child.get("loaders", []):
                    raise ValueError(f"Dependency {project_id or version_id} is incompatible with this profile.")
                resolved_project = child.get("project_id") or project_id
                resolved_version = child.get("id")
                if resolved_project and resolved_version:
                    previous_version = required_versions.setdefault(resolved_project, resolved_version)
                    if previous_version != resolved_version:
                        raise ValueError(f"Dependency {resolved_project} is required at two different versions.")
                if resolved_project and resolved_project in visited:
                    visited.add(key)
                    continue
                hashes = {
                    (item.get("hashes", {}).get("sha1") or "").lower()
                    for item in child.get("files", [])
                } - {""}
                if hashes & local_hashes:
                    visited.add(key)
                    if resolved_project:
                        visited.add(resolved_project)
                    continue
                await visit(child, depth + 1)
                if resolved_project:
                    try:
                        project = await provider.project(resolved_project)
                    except httpx.HTTPError:
                        project = {"id": resolved_project, "title": child.get("name") or resolved_project}
                    projects.append(project)
                resolved.append(child)
                visited.add(key)
                if resolved_project:
                    visited.add(resolved_project)
            finally:
                active.discard(key)

    await visit(root_version, 1)
    return resolved, projects
