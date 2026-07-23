Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: bayesian-estimation

Description: >-

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## When to Use This Skill
Use when the user is:
- Specifying priors and setting up a Bayesian model in Stan, PyMC, NumPyro, brms, or rstanarm
- Running MCMC and diagnosing R-hat, ESS, divergences, or trace plots
- Implementing hierarchical (multilevel) models with partial pooling
- Adding Bayesian inference to a structural model (BLP, dynamic discrete choice, DSGE)
- Reporting credible intervals, posterior predictive checks, or model comparison statistics
- Eliciting priors from calibration targets or literature benchmarks
- Debugging sampling pathologies: divergences, low acceptance rates, poor mixing

Skip when:
- The model is large-N and well-identified (frequentist MLE/GMM is more efficient and faster)
- The task is pure structural estimation without Bayesian components (use `structural-modeling` skill)
- The user needs classical causal inference (use `causal-inference` skill)

## Integration with compound-science
- `numerical-auditor`: Review MCMC convergence diagnostics — R-hat, ESS, divergences. Report format should include all five convergence metrics.
- `econometric-reviewer`: Review prior elicitation strategy, sensitivity analysis, and whether priors are consistent with identification. Use for prior predictive checks and moment-matching to literature targets.
- `methods-explorer`: Find literature calibration targets to set informative prior means. Ask for point estimates and uncertainty ranges, not just means.
- `econometric-reviewer`: Verify that reported posterior means, credible intervals, and model comparison statistics match the actual ArviZ/Stan output.

- `structural-modeling`: Frequentist counterpart — NFXP, MPEC, BLP, dynamic discrete choice. Use Bayesian skills on top of the structural model framework when small samples or hierarchical structure warrants it.
- `causal-inference`: For reduced-form causal methods. Bayesian DiD and RD designs follow the same identification logic; the Bayesian layer adds partial pooling and uncertainty propagation.

- `empirical-playbook` skill (`diagnostic-battery.md`): Convergence diagnostics (R-hat, ESS, divergences) are a subset of the full diagnostic battery
- `numerical-auditor` agent: Prior predictive simulation is a special case of the Mont
