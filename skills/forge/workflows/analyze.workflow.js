export const meta = {
  name: 'forge-analyze-attribution',
  description: 'Adversarial attribution over a metric trend: trend readers + refuters + cited synthesis with provenance footer',
  phases: [{ title: 'Read' }, { title: 'Refute' }, { title: 'Synthesize' }],
}
// args: { analysisPath, shippedFeatures }  — analysis.json produced by `forge analyze`
const A = args && args.analysis ? args.analysis : {}
const shipped = (args && args.shippedFeatures) || []
phase('Read')
const readers = await parallel([
  () => agent('Read this metric analysis JSON and state, in 2 sentences, what the trend is and how strong it is. Be literal; do not invent direction. ANALYSIS: ' + JSON.stringify(A), { label: 'trend-read', phase: 'Read' }),
  () => agent('Given the metric trend and these shipped features ' + JSON.stringify(shipped) + ', list the 2 most plausible CAUSAL drivers of the movement. ANALYSIS: ' + JSON.stringify(A), { label: 'driver-hypo', phase: 'Read' }),
])
phase('Refute')
const refutes = await parallel(readers.filter(Boolean).map((r, i) => () =>
  agent('Adversarially REFUTE this attribution claim. Default to refuted=true if the move could be seasonality, a confounder, or noise rather than the shipped features. Preserve the trend direction faithfully; never reverse it. CLAIM: ' + r, { label: 'refute-' + i, phase: 'Refute' })
))
phase('Synthesize')
const verdict = await agent(
  'Synthesize a cited attribution verdict from the readings and refutations. Output must end with a PROVENANCE FOOTER line copying source_tier, as_of, owner, raw_path, evidence_sha256 from the analysis verbatim. Faithfully preserve the trend direction. READINGS: ' + JSON.stringify(readers) + ' REFUTATIONS: ' + JSON.stringify(refutes) + ' ANALYSIS: ' + JSON.stringify(A),
  { label: 'synthesize', phase: 'Synthesize' }
)
return { verdict, provenance: A.provenance || null }
