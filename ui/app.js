/* =================================================================
   Universal Deployer — Dashboard Application Logic
   ================================================================= */

(() => {
    "use strict";

    // ── Configuration ──────────────────────────────────────────────
    const API_BASE = ""; // Use relative paths (works locally and on Render)

    // ── DOM refs ───────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    const repoUrlInput = $("repoUrl");
    const branchInput = $("branch");
    const btnAnalyze = $("btnAnalyze");
    const btnDeploy = $("btnDeploy");
    const inputError = $("inputError");
    const apiStatusEl = $("apiStatus");

    // AWS credentials
    const awsAccessKey = $("awsAccessKey");
    const awsSecretKey = $("awsSecretKey");
    const awsRegion = $("awsRegion");

    // Sections
    const stepAnalysis = $("stepAnalysis");
    const stepArchitecture = $("stepArchitecture");
    const stepDeploy = $("stepDeploy");
    const stepHistory = $("stepHistory");

    // Content areas
    const analysisGrid = $("analysisGrid");
    const healthRow = $("healthRow");
    const securityRow = $("securityRow");
    const reasoningBox = $("reasoningBox");
    const archOptions = $("archOptions");
    const logPanel = $("logPanel");
    const deployStatusBadge = $("deployStatusBadge");
    const deployResult = $("deployResult");
    const deployUrl = $("deployUrl");
    const deployTableBody = $("deployTableBody");

    // ── State ──────────────────────────────────────────────────────
    let currentAnalysis = null;
    let currentArch = null;
    let selectedTarget = null;

    // ── Helpers ────────────────────────────────────────────────────
    function getAwsCredentials() {
        const ak = awsAccessKey?.value?.trim();
        const sk = awsSecretKey?.value?.trim();
        const rg = awsRegion?.value?.trim();
        return { access_key: ak, secret_key: sk, region: rg };
    }

    function checkDeployState() {
        const creds = getAwsCredentials();
        const hasCreds = creds.access_key && creds.secret_key && creds.region;

        if (selectedTarget && hasCreds) {
            btnDeploy.disabled = false;
            btnDeploy.innerHTML = `
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24">
                <path d="M5 12l5 5L20 7" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              Deploy Selected Architecture
            `;
            hideError();
        } else {
            btnDeploy.disabled = true;
            btnDeploy.innerHTML = `
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              &nbsp; Provide AWS credentials to enable deployment.
            `;
            if (selectedTarget && !hasCreds) {
                showError("AWS credentials are required to deploy.");
            }
        }
    }

    [awsAccessKey, awsSecretKey, awsRegion].forEach(el => {
        if (el) {
            el.addEventListener("input", () => {
                if (stepArchitecture.hasAttribute("data-visible")) {
                    checkDeployState();
                }
            });
            el.addEventListener("change", () => {
                if (stepArchitecture.hasAttribute("data-visible")) {
                    checkDeployState();
                }
            });
        }
    });

    // ── API helpers ────────────────────────────────────────────────
    async function api(method, path, body) {
        const opts = {
            method,
            headers: { "Content-Type": "application/json" },
        };
        if (body) opts.body = JSON.stringify(body);

        const res = await fetch(`${API_BASE}${path}`, opts);
        const data = JSON.parse(await res.text());
        if (res.status >= 400) throw new Error(data.error || "API error");
        return data;
    }

    // ── Health check ───────────────────────────────────────────────
    async function checkHealth() {
        try {
            await api("GET", "/health");
            apiStatusEl.innerHTML = '<span class="status-dot status-dot--online"></span><span>API Online</span>';
        } catch {
            apiStatusEl.innerHTML = '<span class="status-dot status-dot--offline"></span><span>API Offline</span>';
        }
    }

    // ── Show / hide helpers ────────────────────────────────────────
    function show(el) { el.hidden = false; el.setAttribute("data-visible", "true"); }
    function hide(el) { el.hidden = true; el.removeAttribute("data-visible"); }
    function showError(msg) { inputError.textContent = msg; inputError.hidden = false; }
    function hideError() { inputError.hidden = true; }

    // ── Step 1: Analyze ────────────────────────────────────────────
    btnAnalyze.addEventListener("click", async () => {
        hideError();
        const repoUrl = repoUrlInput.value.trim();
        const branch = branchInput.value.trim() || "main";

        if (!repoUrl) return showError("Please enter a GitHub repository URL.");
        if (!/^https:\/\/github\.com\/.+\/.+/.test(repoUrl))
            return showError("URL must be https://github.com/<owner>/<repo>");

        btnAnalyze.classList.add("btn--loading");
        btnAnalyze.disabled = true;

        try {
            // 1. Analyze
            const analysis = await api("POST", "/analyze-repo", { repo_url: repoUrl, branch });
            currentAnalysis = analysis;
            renderAnalysis(analysis);
            show(stepAnalysis);

            // 2. Architecture options
            const arch = await api("POST", "/architecture-options", { analysis });
            currentArch = arch;
            renderArchitecture(arch);
            show(stepArchitecture);
        } catch (err) {
            showError(err.message || "Analysis failed. Is the API running?");
        } finally {
            btnAnalyze.classList.remove("btn--loading");
            btnAnalyze.disabled = false;
        }
    });

    // ── Render analysis ────────────────────────────────────────────
    function renderAnalysis(a) {
        const confidencePct = Math.round((a.confidence_score || 0) * 100);
        const buildType = a.build_system?.type || "—";
        const buildCmd = a.build_system?.build_cmd || "none";

        analysisGrid.innerHTML = [
            chip("Language", a.language, true),
            chip("Framework", a.framework || "—"),
            chip("Repo Type", (a.repo_type || "").replace(/_/g, " ")),
            chip("Dockerized", a.dockerized ? "Yes" : "No"),
            chip("Build System", buildType),
            chip("Confidence", `${confidencePct}%`, confidencePct >= 70),
        ].join("");

        // Health badges
        const h = a.health || {};
        healthRow.innerHTML = [
            healthBadge("README", h.has_readme),
            healthBadge("Tests", h.has_tests),
            healthBadge("CI/CD", h.has_ci),
            healthBadge("License", h.has_license),
            h.has_env_file ? healthBadge(".env exposed", false) : "",
        ].join("");

        // Security warnings
        const sec = a.security_warnings || [];
        if (sec.length) {
            securityRow.innerHTML = `⚠️ <strong>${sec.length} potential secret(s) detected:</strong> ` +
                sec.map(s => `<code>${s.file}:${s.line}</code>`).join(", ");
            show(securityRow);
        } else {
            hide(securityRow);
        }
    }

    function chip(label, value, accent) {
        return `<div class="analysis-chip">
      <div class="analysis-chip__label">${label}</div>
      <div class="analysis-chip__value ${accent ? "analysis-chip__value--accent" : ""}">${value}</div>
    </div>`;
    }

    function healthBadge(label, ok) {
        const cls = ok ? "health-badge--ok" : "health-badge--warn";
        const icon = ok ? "✓" : "✗";
        return `<span class="health-badge ${cls}">${icon} ${label}</span>`;
    }

    // ── Render architecture ────────────────────────────────────────
    function renderArchitecture(arch) {
        reasoningBox.textContent = (arch.reasoning || "").replace(/\*\*/g, "");

        selectedTarget = arch.recommended;
        archOptions.innerHTML = (arch.options || []).map(opt => {
            const rec = opt.architecture_id === selectedTarget ? "arch-card--recommended" : "";
            const sel = opt.architecture_id === selectedTarget ? "arch-card--selected" : "";
            return `<div class="arch-card ${rec} ${sel}" data-key="${opt.architecture_id}">
        <div class="arch-card__name">${opt.name}</div>
        <div class="arch-card__meta">
          ${(opt.aws_services || []).map(s => `<span class="arch-card__tag">${s}</span>`).join("")}
        </div>
        <div class="arch-card__desc">${opt.description}</div>
        <div class="arch-card__cost">💰 ${opt.cost_estimate} &nbsp;·&nbsp; Complexity ${opt.complexity}/5</div>
      </div>`;
        }).join("");

        // Click to select
        archOptions.querySelectorAll(".arch-card").forEach(card => {
            card.addEventListener("click", () => {
                archOptions.querySelectorAll(".arch-card").forEach(c => c.classList.remove("arch-card--selected"));
                card.classList.add("arch-card--selected");
                selectedTarget = card.dataset.key;
                checkDeployState();
            });
        });

        checkDeployState();
    }

    // ── Step 4: Deploy ─────────────────────────────────────────────
    btnDeploy.addEventListener("click", async () => {
        if (!selectedTarget || !currentAnalysis) return;

        btnDeploy.disabled = true;
        show(stepDeploy);
        logPanel.innerHTML = "";
        hide(deployResult);
        setDeployStatus("started");

        const repoUrl = repoUrlInput.value.trim();
        const branch = branchInput.value.trim() || "main";

        try {
            const payload = {
                repo_url: repoUrl,
                branch,
                target: selectedTarget,
            };

            const creds = getAwsCredentials();
            if (!creds || !creds.access_key || !creds.secret_key || !creds.region) {
                showError("AWS credentials are required to deploy.");
                setDeployStatus("failed");
                return;
            }
            payload.aws_credentials = creds;

            const result = await api("POST", "/deploy", payload);

            renderDeployResult(result);
            refreshHistory();
        } catch (err) {
            appendLog(`❌ ${err.message}`, "error");
            setDeployStatus("failed");
        }
    });

    function renderDeployResult(dep) {
        // Logs
        (dep.logs || []).forEach(line => {
            let cls = "info";
            if (line.includes("✅")) cls = "ok";
            else if (line.includes("❌") || line.includes("FAILED")) cls = "error";
            else if (line.includes("⚠️")) cls = "warn";
            else if (line.includes("[") && line.includes("/7]")) cls = "step";
            appendLog(line, cls);
        });

        setDeployStatus(dep.status || "unknown");

        if (dep.url && dep.status === "success") {
            deployUrl.textContent = dep.url;
            deployUrl.href = dep.url;
            show(deployResult);
        }
    }

    function appendLog(text, cls) {
        const div = document.createElement("div");
        div.className = `log-line log-line--${cls || "info"}`;
        div.textContent = text;
        logPanel.appendChild(div);
        logPanel.scrollTop = logPanel.scrollHeight;
    }

    function setDeployStatus(status) {
        deployStatusBadge.textContent = status.replace(/_/g, " ");
        deployStatusBadge.className = `deploy-status deploy-status--${status}`;
    }

    // ── History ────────────────────────────────────────────────────
    btnRefreshHistory.addEventListener("click", refreshHistory);

    async function refreshHistory() {
        try {
            const list = await api("GET", "/deployments");
            if (!list.length) return;
            show(stepHistory);
            deployTableBody.innerHTML = list.map(d =>
                `<tr>
           <td><code>${d.id}</code></td>
           <td title="${d.repo_url}">${d.repo_url.split("/").slice(-1)}</td>
           <td><code>${d.target}</code></td>
           <td><span class="deploy-status deploy-status--${d.status}">${d.status}</span></td>
           <td>${d.url ? `<a href="${d.url}" target="_blank" class="result-url">Link</a>` : "—"}</td>
           <td><button class="btn btn--small btn--secondary" onclick="viewLogs('${d.id}')">Logs</button></td>
         </tr>`
            ).join("");
        } catch { /* ignore */ }
    }

    window.viewLogs = async (id) => {
        try {
            const dep = await api("GET", `/deployment-status/${id}`);
            show(stepDeploy);
            logPanel.innerHTML = "";
            renderDeployResult(dep);
            stepDeploy.scrollIntoView({ behavior: "smooth" });
        } catch (err) {
            alert("Failed to load logs: " + err.message);
        }
    };

    // ── Enter key triggers analyze ─────────────────────────────────
    repoUrlInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") btnAnalyze.click();
    });

    // ── Init ───────────────────────────────────────────────────────
    checkHealth();
    refreshHistory(); // Load previous deployments immediately
    setInterval(checkHealth, 15000);

})();
