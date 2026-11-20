{{- define "slo.labels" -}}
app.kubernetes.io/part-of: slo-loop-lab
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* repo@digest when a digest is pinned, otherwise repo:tag */}}
{{- define "slo.image" -}}
{{- if .digest -}}{{ .repository }}@{{ .digest }}{{- else -}}{{ .repository }}:{{ .tag }}{{- end -}}
{{- end -}}

{{- define "slo.policyCheck" -}}
{{- if not (has .Values.autoscaling.policy (list "static1" "static2" "static3" "cpu" "queue" "burn")) -}}
{{- fail (printf "autoscaling.policy must be one of static1|static2|static3|cpu|queue|burn, got %q" .Values.autoscaling.policy) -}}
{{- end -}}
{{- end -}}

{{- define "slo.isKeda" -}}
{{- if has .Values.autoscaling.policy (list "cpu" "queue" "burn") -}}true{{- end -}}
{{- end -}}

{{- define "slo.staticReplicas" -}}
{{- trimPrefix "static" .Values.autoscaling.policy -}}
{{- end -}}

{{/* Non-root, no privilege escalation: for the two Python services (the llama.cpp image runs as its own user). */}}
{{- define "slo.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "slo.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
{{- end -}}
