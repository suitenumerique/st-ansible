# suitenumerique\.st Release Notes

**Topics**

- <a href="#v0-5-1">v0\.5\.1</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
- <a href="#v0-5-0">v0\.5\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#minor-changes-1">Minor Changes</a>
    - <a href="#breaking-changes">Breaking Changes</a>
    - <a href="#deprecated-features">Deprecated Features</a>
- <a href="#v0-4-0">v0\.4\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#minor-changes-2">Minor Changes</a>
    - <a href="#bugfixes-1">Bugfixes</a>
- <a href="#v0-3-1">v0\.3\.1</a>
    - <a href="#minor-changes-3">Minor Changes</a>
- <a href="#v0-3-0">v0\.3\.0</a>
    - <a href="#release-summary-2">Release Summary</a>
    - <a href="#minor-changes-4">Minor Changes</a>
    - <a href="#bugfixes-2">Bugfixes</a>
    - <a href="#new-plugins">New Plugins</a>
        - <a href="#callback">Callback</a>
- <a href="#v0-2-2">v0\.2\.2</a>
    - <a href="#minor-changes-5">Minor Changes</a>
    - <a href="#bugfixes-3">Bugfixes</a>
- <a href="#v0-2-1">v0\.2\.1</a>
    - <a href="#minor-changes-6">Minor Changes</a>
- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary-3">Release Summary</a>
    - <a href="#minor-changes-7">Minor Changes</a>
    - <a href="#bugfixes-4">Bugfixes</a>
- <a href="#v0-1-1">v0\.1\.1</a>
    - <a href="#minor-changes-8">Minor Changes</a>
    - <a href="#bugfixes-5">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-4">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-9">Minor Changes</a>

<a id="v0-5-1"></a>
## v0\.5\.1

<a id="minor-changes"></a>
### Minor Changes

* Update dependency molecule to v26\.10\.0
* Update dependency typer to v0\.27\.3
* Update docker\.io/caddy Docker tag to v2\.11\.7
* Update ghcr\.io/google/cadvisor Docker tag to v0\.60\.6

<a id="bugfixes"></a>
### Bugfixes

* st\-cli \- read\_inventory normalises the section header of the hosts file\. A legacy socks\-proxy group written before 0\.5\.0 now matches the group socks\_proxy\, so the unit targets its hosts again\. An upgrade flag replays messages/\<env\>/socks\-proxy and rewrites the hosts file with the group socks\_proxy and the aliases socks\_proxy\<n\>\.

<a id="v0-5-0"></a>
## v0\.5\.0

<a id="release-summary"></a>
### Release Summary

Added file\-scanner and transfers\. Added IP filtering on Keycloak admin path
and Django Admin paths\. Bump podman compose to 1\.6\.0\. Deprecate messages mta\-in\,
added messages pymta\.

<a id="minor-changes-1"></a>
### Minor Changes

* Update dependency ansible\-core to v2\.21\.5
* Update dependency cryptography to v50\.0\.2
* Update dependency molecule to v26\.9\.0
* Update dependency pytest\-mock to v3\.16\.0
* Update dependency python to 3\.14
* Update dependency suitenumerique/meet to v1\.33\.0
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.7
* Update docker\.io/rspamd/rspamd Docker tag to 4\.2\.0
* Update docker\.io/valkey/valkey Docker tag to v9\.1\.2
* Update ghcr\.io/suitenumerique/messages Docker tag to 0\.10\.0
* cli\: <code>st\-cli bootstrap messages</code> bootstraps <code>pymta</code> instead of <code>mta\-in</code>\. The pymta env blob asks for <code>PYMTA\_SMTP\_HOSTNAME</code>\.
* cli\: the keycloak env template no longer sets <code>KC\_HTTP\_ENABLED</code> and <code>KC\_PROXY\_HEADERS</code>\, because the image sets them\. The rendered env requires image tag 0\.10\.0 or later\.
* docs\: add <code>docs/04\-messages/06\-pymta\.md</code>\. The full\-high\-availability example deploys pymta \(<code>playbook\_pymta\.yml</code>\) instead of mta\-in\.
* drive\: the backend healthcheck passes the <code>Host</code> header without inner quotes\. podman\-compose 1\.5\.0 and later exec the <code>CMD</code> list without a shell\, so the quotes reached <code>wget</code> and the probe failed\.
* file\-scanner\: the worker gets a healthcheck that pings the dramatiq broker \(<code>redis\.from\_url\(config\.get\_settings\(\)\.worker\_broker\_url\)\.ping\(\)</code>\)\, run as a <code>CMD</code> list in the distroless image\. Until now the worker had no probe at all\, so a worker left running and silent on an unreachable <code>WORKER\_BROKER\_URL</code> counted as healthy and the async scans never completed\. dramatiq exposes no worker ping\, so this checks the broker connection\, not the consumer\. It requires podman\-compose 1\.5\.0 or later\; on a host that keeps 1\.3\.0 the probe is wrapped in <code>/bin/sh \-c</code>\, which the image has no shell for\, and the unit does not start\.
* file\_scanner role \- add a <code>file\_scanner</code> role deploying the file\-scanner antivirus service \(suitenumerique/file\-scanner\) \- a FastAPI REST API and a dramatiq worker with a bundled ClamAV daemon\, all in one compose unit on the rootless podman base\. Unlike the upstream <code>docker\-compose\.yml</code> the role bundles no Redis \- the dramatiq broker is an external Redis 7\+ prompted at bootstrap \(<code>WORKER\_BROKER\_URL</code>\, routed through the secret backend since it can embed a password\)\, consistent with every other app of the collection\. No PostgreSQL\, S3 or IdP needed\; used by Transfers to scan completed uploads\. The worker\'s download scratch is a host directory \(<code>downloads/</code> under the application directory\, so the space is accounted for and can be put on its own filesystem\) and its concurrency defaults to 1 process x 4 threads against upstream\'s 2 x 8 \(<code>st\_file\_scanner\_worker\_processes</code> / <code>st\_file\_scanner\_worker\_threads</code>\) because every in\-flight job holds a full copy of the file it scans\, and clamd spools the same bytes again\, so the default bounds a scan burst at \~17 GB of transient disk instead of \~64 GB\.
* file\_scanner\: optional second malware engine \- <code>st\_file\_scanner\_exav\_enabled</code> adds an exav daemon \(<code>ghcr\.io/sylvinus/exav</code>\) to the compose unit\, on the clamd protocol like clamav\. Deploying it changes no verdict by itself\: the application reaches it once <code>EXAV\_HOSTS</code> and <code>DEFAULT\_SCANNERS</code> name it in <code>st\_file\_scanner\_env</code>\. It ships no signature database\, so point <code>EXAV\_DB\_URL</code> at a prebuilt <code>\.exavdb</code> \(loads in seconds\)\; without it the daemon waits for signatures and stays unhealthy\, which is why the API and the worker are ordered after exav but never gated on its health\. Compiling the signatures the bundled clamav keeps fresh is the other path\, at \~3\.6 GB of RAM on every start and a raised <code>st\_file\_scanner\_timeout</code>\. The clamav defaults also gain <code>MaxScanTime</code> and <code>AlertExceedsMax</code>\, without which clamd answers OK on a scan it could not finish and the file is reported clean unscanned\.
* file\_scanner\: the clamav container is now configurable through a new <code>st\_file\_scanner\_clamav\_env</code> blob\, empty by default and falling back to the role\'s built\-in tuning \(the image maps <code>CLAMD\_CONF\_\<Option\></code>/<code>FRESHCLAM\_CONF\_\<Option\></code> lines onto clamd\.conf/freshclam\.conf\)\. The default raises clamd\'s size caps \(<code>StreamMaxLength</code>/<code>MaxFileSize</code>/<code>MaxScanSize</code> to 2200M\) above the scanner\'s 2 GiB <code>MAX\_URL\_SIZE</code> — with clamd\'s stock limits a big file either failed mid\-INSTREAM on every retry or\, above MaxFileSize/MaxScanSize\, was skipped and reported clean without being scanned\.
* keycloak\: add <code>st\_keycloak\_http\_port</code> \(default <code>50201</code>\) and <code>st\_keycloak\_management\_port</code> \(default <code>50202</code>\)\. The default compose template sets <code>KC\_HTTP\_PORT</code> and <code>KC\_HTTP\_MANAGEMENT\_PORT</code> from them\, so with <code>network\_mode\: host</code> Keycloak listens on <code>127\.0\.0\.1\:50201</code> behind Caddy and the management interface \(<code>/health</code>\, <code>/metrics</code>\) listens on <code>50202</code> instead of <code>9000</code>\, inside the <code>502xx</code> block of the collection port scheme\. Update firewall rules and Prometheus scrape targets that used port <code>9000</code>\. The compose <code>environment</code> block sets <code>PORT</code>\, <code>KC\_HTTP\_PORT</code> and <code>KC\_HTTP\_MANAGEMENT\_PORT</code> and overrides these keys in <code>st\_keycloak\_env</code>\; use the role variables instead\.
* keycloak\: document the <code>KEYCLOAK\_ADMIN\_IP\_ALLOWLIST</code> and <code>KEYCLOAK\_TRUSTED\_PROXIES</code> variables of the image\, and the host ports the image binds with <code>network\_mode\: host</code>\.
* messages\: add the <code>pymta</code> component \(<code>ghcr\.io/suitenumerique/messages\-mta\-in\-py</code>\)\, a pure\-Python inbound MTA that replaces mta\-in\. It runs with <code>network\_mode\: host</code>\. The role sets <code>PYMTA\_SMTP\_BIND\_PORT</code> from <code>st\_messages\_pymta\_port</code> \(default <code>50425</code>\)\, binds the metrics endpoint to <code>127\.0\.0\.1</code> on <code>st\_messages\_pymta\_metrics\_port</code> \(default <code>50426</code>\)\, and enables STARTTLS from <code>st\_messages\_pymta\_starttls\_certificate\_path</code>\.
* messages\: the workers compose template runs the Celery ping healthcheck as a <code>CMD</code> list instead of <code>CMD\-SHELL</code>\, so it works with the distroless backend image that has no shell\. With podman\-compose 1\.3\.0 the list still runs through <code>/bin/sh</code>\, so the behavior on a host that keeps 1\.3\.0 is unchanged\.
* podman role\: the application start script now fails when <code>podman\-compose up \-d</code> created fewer containers than the compose file declares\. It can exit 0 having skipped a service whose <code>depends\_on</code> it gave up waiting for\, and the unit then reported every container healthy with half the stack missing \- a file\-scanner whose API and worker were never created while systemd showed the service as running\.
* podman\: the role installs podman\-compose <code>1\.6\.0\-1</code>\, resolved on snapshot\.debian\.org from <code>st\_podman\_compose\_version</code>\, instead of the trixie package 1\.3\.0\. The role installs a package from an operator URL when you set <code>st\_podman\_compose\_deb\_url</code> and <code>st\_podman\_compose\_deb\_checksum</code>\. The checksum is required\. Version 1\.3\.0 runs every compose healthcheck through <code>/bin/sh \-c</code>\; 1\.5\.0 and later exec a <code>CMD</code> list directly\, so images without a shell can have a healthcheck\. The install step does not restart any unit\; a container gets the new form when its unit restarts\. A host that already has a newer version keeps it\.
* st\-cli \- add <code>file\-scanner</code> app support to bootstrap with its own non\-Django questionnaire \(external dramatiq broker URL\, caller public keys in <code>JWT\_ISSUER\_KEYS</code>\, generated Ed25519 webhook signing seed and /metrics bearer token kept in the vault\, optional <code>ALLOWED\_URL\_HOSTS</code> allowlist and the separate <code>SSRF\_ALLOWED\_HOSTS</code> list of hosts allowed to resolve to a private address\, which must name the webhook callback host as well as any internal object store\, because one SSRF\-guarded client serves both legs of an async job\)\. Inventory group names are dash\-normalised \(<code>\[file\_scanner\]</code>\, alias <code>file\_scanner1</code>\) so ansible stops warning about invalid group characters\. The pre\-bootstrap Requirements box is now app\-tailored via a <code>requirements</code> list in each app manifest \(file\-scanner shows only its hosts\, broker and caller keys\, keycloak no Redis/S3/OIDC ones\, messages adds OpenSearch and MX/SPF/DKIM DNS prep\, meet the LiveKit/TURN domains\)\; the list is mandatory per app manifest\, replacing the old generic one\.
* st\-cli \- add <code>transfers</code> app support \(backend core \+ workers\) to bootstrap\, reusing the django\-lasuite base env with transfers\-specific overrides \(<code>transfers\.settings</code> module\, frontend Caddy S3 origin\, <code>USE\_X\_FORWARDED\_FOR</code>\, optional Drive file\-picker integration\, and an optional file\-scanner / antivirus integration — the <code>SCAN\_\*</code> keys with the EdDSA signing key kept in the vault and the tuning knobs left to their upstream defaults unless answered\)\.
* st\-cli \- new <code>generate\-keypairs APP ENV</code> command minting Ed25519 caller keypairs for the file\-scanner JWT auth \(pure\-python RFC 8032 derivation\, no crypto dependency\) \- asks an issuer name per keypair and prints a wiring summary with the <code>JWT\_ISSUER\_KEYS</code> value \(merged with the bootstrapped unit\'s existing keys\) and each caller\'s private key\, shown once and never stored\. Its <code>\-s/\-\-signing\-key</code> flag mints file\-scanner\'s own webhook signing seed \(<code>JWT\_SIGNING\_KEY</code>\) instead\, printing the seed and the public half advertised at <code>/\.well\-known/jwks\.json</code> \- for the <code>hashi\_vault</code> backend\, which generates nothing\, and for key rotation \(<code>bootstrap</code> generates that seed itself on the ansible\-vault backend\)\.
* transfers role \- add a <code>transfers</code> role deploying the Transfers application \(suitenumerique/transfers\) \- a Django backend\, a Caddy\-served React frontend and an optional Celery worker \(with the beat scheduler embedded\) \- on the rootless podman base\, with S3\-backed uploads and OIDC login\.
* transfers\: the worker gets the Celery ping healthcheck as a <code>CMD</code> list\, replacing <code>healthcheck\: disable\: true</code>\. The probe was disabled because podman\-compose 1\.3\.0 wraps even a <code>CMD</code> list in <code>/bin/sh \-c</code>\, which the distroless backend image has no shell for\; 1\.5\.0 and later exec it directly\. The ping is a broadcast on the broker\, so with several hosts on one Redis another host\'s worker can answer for this one\.

<a id="breaking-changes"></a>
### Breaking Changes

* keycloak\: the default compose template sets <code>PORT</code> instead of <code>KC\_HTTP\_PORT</code>\, because the <code>messages\-keycloak</code> image now runs Caddy in front of Keycloak and Caddy owns the public port\. The role requires image tag 0\.10\.0 or later \(messages PR \#793\)\, and the default tag is now 0\.10\.0\. A custom <code>st\_keycloak\_compose\_template</code> must set <code>PORT</code>\, <code>KC\_HTTP\_PORT</code> and <code>KC\_HTTP\_MANAGEMENT\_PORT</code> too\.

<a id="deprecated-features"></a>
### Deprecated Features

* cli\: <code>st\-cli bootstrap</code> no longer offers <code>mta\-in</code> and <code>\-c mta\-in</code> is not a valid target\. Existing <code>mta\-in</code> units still work with <code>deploy</code>\, <code>remote</code>\, <code>generate</code> and <code>upgrade</code>\.
* messages\: the <code>mta\-in</code> \(Postfix\) component and its <code>st\_messages\_mta\_in\_\*</code> variables are deprecated in favour of <code>pymta</code>\. The role prints a warning when <code>st\_messages\_mta\_in\_enabled</code> is true\. See <code>docs/04\-messages/06\-pymta\.md</code> for the migration steps\.

<a id="v0-4-0"></a>
## v0\.4\.0

<a id="release-summary-1"></a>
### Release Summary

Added cli rebootstrap and improve upgrades\, added an IP filtering setup in front of Django Admins paths\, bump drive\, docs and meet\.

<a id="minor-changes-2"></a>
### Minor Changes

* Update dependency suitenumerique/docs to v5\.6\.1
* Update dependency suitenumerique/drive to v0\.22\.0
* Update dependency suitenumerique/meet to v1\.31\.0
* Update docker\.io/collabora/code Docker tag to 26\.04\.3\.2\.1
* Update docker/build\-push\-action digest to c3c9e26
* Update docker/setup\-buildx\-action digest to f87e599
* Update docker/setup\-qemu\-action digest to 9901266
* cli\: <code>\@openbao\(\)</code> markers typed into a provider prompt are expanded\. <code>st\-cli version</code> prints the pin warning once\.
* cli\: <code>doctor</code> diffs the committed env blobs against the current templates and no longer reports <code>st\_\*</code> vars absent from the role spec\.
* cli\: <code>st\-cli bootstrap</code> on an existing unit replays the questionnaire pre\-filled from the committed tree and merges the result\. Secrets are never rotated\.
* cli\: <code>st\-cli deploy</code> resolves every target host before it deploys a component\. A wrong <code>\-c</code> and <code>\-H</code> pair or an empty inventory now fails before any deploy\.
* cli\: <code>st\-cli upgrade</code> no longer runs <code>pipx upgrade</code> itself\. It prints the install command and stops\.
* cli\: <code>upgrades\.yml</code> accepts <code>version\: next</code> for the flag of an unreleased change\. <code>make version</code> replaces it with the release version\. A <code>next</code> flag replays on every <code>st\-cli upgrade</code> run and never blocks <code>deploy</code>\.
* cli\: <code>upgrades\.yml</code> declares per\-release rebootstrap flags \(<code>apps</code>\, <code>components</code>\, <code>full\_replay</code>\, <code>warnings</code>\) and a <code>baseline</code> stamp\. <code>doctor</code> reports them\, <code>deploy</code> refuses to run until they are cleared\, <code>st\-cli upgrade</code> replays the flagged units and prints the manual steps\.
* cli\: fix the README\, <code>CLAUDE\.md</code> and help texts\. <code>podman rmi</code> removes the image\. <code>st\-cli upgrade</code> does not upgrade the CLI itself\. <code>ST\_CLI\_NO\_UPSTREAM\_CHECK</code> also disables the upstream gate of <code>st\-cli upgrade</code>\.
* cli\: remove dead code\, merge duplicate helpers\, and trim the docstrings to the why\-only rule\. <code>pyproject\.toml</code> commits the ruff rule set\. The <code>ansible</code> extra pins the same <code>ansible\-core</code> as <code>full</code>\.
* cli\: the <code>link</code> key of a flag is optional\. The CLI derives the CHANGELOG anchor from the flag version\.
* cli\: the drive bootstrap writes <code>st\_drive\_caddy\_env</code> and literal S3 values\, like meet and docs\. A 0\.4\.0 flag makes <code>st\-cli upgrade</code> replay drive and print the manual steps\.
* docs\, drive\, meet\: the caddy edge now walks <code>X\-Forwarded\-For</code> right to left \(<code>trusted\_proxies\_strict</code>\)\. A client can no longer set its own client IP with a prepended <code>X\-Forwarded\-For</code> entry\. Django still receives the same <code>X\-Forwarded\-For</code> header as before\.
* docs\, drive\, meet\: the caddy edge reads two optional keys in <code>st\_\<app\>\_caddy\_env</code>\: <code>CADDY\_ADMIN\_IP\_ALLOWLIST</code> \(space\-separated CIDR list of client IPs allowed on <code>/admin</code>\, default allows all\) and <code>CADDY\_TRUSTED\_PROXIES</code> \(proxies whose <code>X\-Forwarded\-For</code> sets the client IP\, default <code>private\_ranges</code>\, the previous hard\-coded value\)\.
* docs\: the example playbooks set the caddy env for drive and meet\. Renovate groups the caddy bumps in one PR\.
* drive\: a <code>drive\-caddy</code> container now owns <code>st\_drive\_port</code> and proxies the frontend\, the backend and the S3 media routes\, like docs and meet\. <code>st\_drive\_nginx\_template</code> and the <code>st\_drive\_s3\_\*</code> vars are replaced by <code>st\_drive\_caddy\_env</code> \(<code>CADDY\_S3\_\*</code>\)\, <code>st\_drive\_caddy\_image</code> and <code>st\_drive\_caddy\_tag</code>\.
* drive\: the collabora compose file no longer sets a healthcheck\. The 26\.04\.3 image has no shell\, and podman\-compose 1\.3\.0 wraps every compose test in <code>/bin/sh \-c</code>\. Podman applies the <code>HEALTHCHECK</code> of the image \(<code>coolwsd \-\-probe</code>\) instead\. A custom <code>st\_drive\_collabora\_compose\_template</code> must drop its <code>healthcheck</code> block too\.

<a id="bugfixes-1"></a>
### Bugfixes

* cli\: <code>st\-cli reset \-H \<alias\></code> redeploys only the selected host\. It redeployed the component on every host\.
* cli\: a blank <code>AWS\_S3\_REGION\_NAME</code> answer on a rebootstrap removes the key and warns\, instead of writing an empty value\.

<a id="v0-3-1"></a>
## v0\.3\.1

<a id="minor-changes-3"></a>
### Minor Changes

* Update dependency suitenumerique/docs to v5\.5\.0
* Update dependency suitenumerique/meet to v1\.29\.0
* Update dependency typer to v0\.27\.2
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.6
* Update docker/setup\-buildx\-action digest to 37fe631

<a id="v0-3-0"></a>
## v0\.3\.0

<a id="release-summary-2"></a>
### Release Summary

Adds two new applications\: Docs \(impress\, with workers and yprovider
sub\-apps\) and Projects \(a Planka fork\)\, both deployable end to end with
st\-cli\. The bootstrap \"Requirements\" checklist is now app\-aware\, and
several dependencies were bumped \(meet 1\.27\.0\, messages 0\.9\.0\, LiveKit\)\.

<a id="minor-changes-4"></a>
### Minor Changes

* 1. added docs app support to st\-cli
* \(docs\) added the docs role\, deploying the Docs \(impress\) application as a caddy \+ frontend \+ backend core\, with workers and yprovider sub\-apps
* \(projects\) new <code>projects</code> role and st\-cli support to deploy Projects \(<code>suitenumerique/projects</code>\, a Planka fork\) — a single Sails\.js container with external PostgreSQL\, OIDC\-enforced SSO \(Keycloak/ProConnect provider switch\) and optional S3\-backed uploads\; <code>st\-cli bootstrap projects \<env\></code> / <code>st\-cli deploy projects \<env\></code> behave like the other apps\.
* \(st\-cli\) the bootstrap \"Requirements\" checklist is now app\-aware — each app declares the external infrastructure it needs via a <code>requirements</code> manifest key — so <code>projects</code> and <code>keycloak</code> operators are no longer told to provision a Redis they never use\.
* Update dependency molecule to v26\.8\.0
* Update dependency suitenumerique/meet to v1\.27\.0
* Update dependency suitenumerique/messages to v0\.9\.0
* Update dependency typer to v0\.27\.1
* Update docker\.io/livekit/egress Docker tag to v1\.14\.1
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.5

<a id="bugfixes-2"></a>
### Bugfixes

* \(st\-cli\) the <code>vars\.yml</code> header no longer claims secrets are stored in an encrypted <code>vault\.yml</code> when the hashi\_vault \(OpenBao\) backend is in use\, where no <code>vault\.yml</code> is written and secrets are referenced by lookup\.

<a id="new-plugins"></a>
### New Plugins

<a id="callback"></a>
#### Callback

* suitenumerique\.st\.compact \- Compact one\-line\-per\-task output with a live pending line on a TTY\.

<a id="v0-2-2"></a>
## v0\.2\.2

<a id="minor-changes-5"></a>
### Minor Changes

* Update docker\.io/clamav/clamav Docker tag to v1\.5\.4
* Update docker\.io/rspamd/rspamd Docker tag to v4\.1\.4
* Update docker\.io/valkey/valkey Docker tag to v9\.1\.1
* Update docker/login\-action digest to dbcb813
* cli\: <code>st\-cli upgrade</code> now checks upstream first and\, without pipx \(container installs\)\, tells the user to run <code>docker pull ghcr\.io/suitenumerique/st\-cli\:latest</code> and re\-run <code>st\-cli upgrade</code> instead of the pip hint
* cli\: the behind\-upstream warning on other commands now branches on pipx — without pipx it names the exact <code>docker pull ghcr\.io/suitenumerique/st\-cli\:latest</code> command to run before <code>st\-cli upgrade</code>
* plugins\: the compact callback now prints the <code>msg</code> of a <code>debug</code> task in a green box in place of the ok line and the JSON dump

<a id="bugfixes-3"></a>
### Bugfixes

* cli\: only push latest on new tags to follow the st\-cli collection tags
* plugins\: the compact callback no longer concatenates the pending task lines on one row when a task runs on multiple hosts\, and it keeps a live pending line while other hosts still run

<a id="v0-2-1"></a>
## v0\.2\.1

<a id="minor-changes-6"></a>
### Minor Changes

* callback\: added the suitenumerique\.st\.compact stdout callback\, one line per task and host\, a live progress line on a TTY\, diffs for changed tasks\, and full default\-style error output
* cli\: the generated ansible\.cfg now selects suitenumerique\.st\.compact as the stdout callback
* cli\: the generated ansible\.cfg silences the Python interpreter discovery warning with <code>interpreter\_python \= auto\_silent</code>

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary-3"></a>
### Release Summary

Adds support for meet recordings\, fix openbao markers on st\-cli bootstrap
and multiple versions upgrades\.

<a id="minor-changes-7"></a>
### Minor Changes

* Update actions/checkout digest to 3d3c42e
* Update actions/setup\-python action to v7
* Update dependency ansible\.posix to v2\.2\.2
* Update dependency containers\.podman to v1\.20\.2
* Update dependency suitenumerique/meet to v1\.23\.0
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.4
* Update docker/login\-action digest to abd2ef4
* meet\: added custom logo handling
* meet\: separated egress component and added recording feature to bootstrap

<a id="bugfixes-4"></a>
### Bugfixes

* cli\: allow \@openbao markers on non\-secret fields

<a id="v0-1-1"></a>
## v0\.1\.1

<a id="minor-changes-8"></a>
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

<a id="bugfixes-5"></a>
### Bugfixes

* Fixed restic install task for upgrade workflow

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-4"></a>
### Release Summary

Added st\-cli to manage LST environments bootstraps and deployments\, refactored roles to make single\-host deployments easier\, started the CHANGELOG dance\.

<a id="major-changes"></a>
### Major Changes

* cli\: added st\-cli \#27
* roles\: refactor every uid\, gid and ports to allow single\-host deployments \#26

<a id="minor-changes-9"></a>
### Minor Changes

* changelog\: added antsibull\-changelog config\, Makefile targets and CI job \#13
