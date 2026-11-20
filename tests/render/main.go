// Offline chart renderer used by the test-suite when `helm` is not available (e.g. CI sandboxes).
//
// It runs the chart templates through Go's REAL text/template engine, so template syntax, whitespace
// trimming ({{- -}}) and control flow behave exactly as in Helm. Only the function library is
// re-implemented, and only the subset the chart uses (see funcs below). It is a safety net for
// development, NOT a replacement for `helm lint` / `helm template`, which `make lint` runs when helm
// is installed.
//
// usage: go run main.go <chartDir> <values.json> <releaseName> <namespace>
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"text/template"
)

type files struct{ root string }

func (f files) Get(name string) string {
	b, err := os.ReadFile(filepath.Join(f.root, name))
	if err != nil {
		panic(fmt.Sprintf("Files.Get(%q): %v", name, err))
	}
	return string(b)
}

func toStr(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case json.Number:
		return t.String()
	default:
		return fmt.Sprint(t)
	}
}

// toYAML emits JSON, which is valid YAML (flow style). Sufficient for structural tests.
func toYAML(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func indentStr(n int, s string) string {
	pad := strings.Repeat(" ", n)
	return pad + strings.ReplaceAll(s, "\n", "\n"+pad)
}

func truthy(v interface{}) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case json.Number:
		f, _ := t.Float64()
		return f != 0
	case []interface{}:
		return len(t) > 0
	case map[string]interface{}:
		return len(t) > 0
	}
	return true
}

func main() {
	if len(os.Args) != 5 {
		fmt.Fprintln(os.Stderr, "usage: main <chartDir> <values.json> <release> <namespace>")
		os.Exit(2)
	}
	chartDir, valuesPath, release, namespace := os.Args[1], os.Args[2], os.Args[3], os.Args[4]

	var values map[string]interface{}
	vb, err := os.ReadFile(valuesPath)
	if err != nil {
		panic(err)
	}
	dec := json.NewDecoder(bytes.NewReader(vb))
	dec.UseNumber()
	if err := dec.Decode(&values); err != nil {
		panic(err)
	}
	chart := map[string]interface{}{"Name": "slo-loop-app", "Version": "0.1.0", "AppVersion": "0.1.0"}

	root := template.New("root")
	funcs := template.FuncMap{
		"quote": func(v interface{}) string { return strconv.Quote(toStr(v)) },
		"toYaml": toYAML,
		"indent": func(n int, s string) string { return indentStr(n, s) },
		"nindent": func(n int, s string) string { return "\n" + indentStr(n, s) },
		"printf": fmt.Sprintf,
		"list": func(items ...interface{}) []interface{} { return items },
		// Same semantics as Sprig: has(needle, haystack); trimPrefix(prefix, s).
		"has": func(needle interface{}, hay []interface{}) bool {
			for _, h := range hay {
				if toStr(h) == toStr(needle) {
					return true
				}
			}
			return false
		},
		"trimPrefix": func(prefix, s string) string { return strings.TrimPrefix(s, prefix) },
		"default": func(d, v interface{}) interface{} {
			if truthy(v) {
				return v
			}
			return d
		},
		"required": func(msg string, v interface{}) (interface{}, error) {
			if !truthy(v) {
				return nil, fmt.Errorf("%s", msg)
			}
			return v, nil
		},
		"fail": func(msg string) (string, error) { return "", fmt.Errorf("%s", msg) },
		"include": func(name string, data interface{}) (string, error) {
			var buf bytes.Buffer
			if err := root.ExecuteTemplate(&buf, name, data); err != nil {
				return "", err
			}
			return buf.String(), nil
		},
	}
	root.Funcs(funcs).Option("missingkey=error")

	tplDir := filepath.Join(chartDir, "templates")
	entries, err := os.ReadDir(tplDir)
	if err != nil {
		panic(err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for _, n := range names {
		b, err := os.ReadFile(filepath.Join(tplDir, n))
		if err != nil {
			panic(err)
		}
		if _, err := root.New(n).Parse(string(b)); err != nil {
			fmt.Fprintf(os.Stderr, "parse error in %s: %v\n", n, err)
			os.Exit(1)
		}
	}

	data := map[string]interface{}{
		"Values":  values,
		"Release": map[string]interface{}{"Name": release, "Namespace": namespace, "Service": "Helm"},
		"Chart":   chart,
		"Files":   files{root: chartDir},
	}
	for _, n := range names {
		if strings.HasPrefix(n, "_") {
			continue
		}
		var buf bytes.Buffer
		if err := root.ExecuteTemplate(&buf, n, data); err != nil {
			fmt.Fprintf(os.Stderr, "render error in %s: %v\n", n, err)
			os.Exit(1)
		}
		out := strings.TrimSpace(buf.String())
		if out == "" {
			continue
		}
		fmt.Printf("---\n# Source: slo-loop-app/templates/%s\n%s\n", n, out)
	}
}
