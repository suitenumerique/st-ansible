# suitenumerique\.st Release Notes

**Topics**

- <a href="#v0-1-1">v0\.1\.1</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-1">Minor Changes</a>

<a id="v0-1-1"></a>
## v0\.1\.1

<a id="minor-changes"></a>
### Minor Changes

* Added Renovate configuration \(<code>renovate\.json5</code>\) and renovate Makefile target
* Pinned the st\-cli and molecule\-lima Python dependencies to exact versions
* Update actions/checkout action to v7
* Update dependency restic/restic to v0\.19\.1
* Update docker/build\-push\-action action to v7
* Update docker/login\-action action to v4
* Update docker/metadata\-action action to v6
* Update docker/setup\-buildx\-action action to v4
* Update docker/setup\-qemu\-action action to v4

<a id="bugfixes"></a>
### Bugfixes

* Fixed restic install task for upgrade workflow

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary"></a>
### Release Summary

Added st\-cli to manage LST environments bootstraps and deployments\, refactored roles to make single\-host deployments easier\, started the CHANGELOG dance\.

<a id="major-changes"></a>
### Major Changes

* 1. added st\-cli \#27
* \(roles\) refactor every uid\, gid and ports to allow single\-host deployments \#26

<a id="minor-changes-1"></a>
### Minor Changes

* \(changelog\) added antsibull\-changelog config\, Makefile targets and CI job \#13
