// Command protogen derives the public Policy resource fields from the Kubernetes API types.
// Proto field numbers are explicit tags on those types so adding or reordering Go fields cannot
// silently change the wire contract.
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"reflect"
	"strconv"
	"strings"
	"unicode"
)

var types = []string{
	"RoleRef", "Rule", "SandboxTargets", "SandboxTarget", "CredentialRef", "ActionPolicy", "PolicySetSpec", "MissingRole", "Status", "IdentityRef", "Subject", "PolicyBindingSpec",
	"IdentitySpec", "KubernetesRule", "KubernetesRoleSetSpec", "ServiceAccountTarget",
	"ServiceAccountGrantSpec", "ServiceAccountGrantStatus", "ManagedServiceAccountSpec", "CreatedServiceAccount",
	"ManagedServiceAccountStatus",
}

func main() {
	if len(os.Args) != 2 {
		panic("usage: protogen types.go")
	}
	f, err := parser.ParseFile(token.NewFileSet(), os.Args[1], nil, parser.ParseComments)
	if err != nil {
		panic(err)
	}
	structs := make(map[string]*ast.StructType)
	for _, decl := range f.Decls {
		gd, ok := decl.(*ast.GenDecl)
		if !ok {
			continue
		}
		for _, spec := range gd.Specs {
			if t, ok := spec.(*ast.TypeSpec); ok {
				if st, ok := t.Type.(*ast.StructType); ok {
					structs[t.Name.Name] = st
				}
			}
		}
	}
	fmt.Print(`// Generated from agentplane/policy/api/v1alpha1/types.go. Do not edit.
syntax = "proto3";
package agentplane.policy.resource.v1;
option go_package = "github.com/agentydragon/ducktape/agentplane/policy/api/resourcev1;resourcev1";
import "google/protobuf/struct.proto";
import "google/protobuf/timestamp.proto";

// Kubernetes condition status, including the generation this reader observed.
message Condition {
  string type = 1;
  string status = 2;
  int64 observed_generation = 3;
  google.protobuf.Timestamp last_transition_time = 4;
  string reason = 5;
  string message = 6;
}

`)
	for _, name := range types {
		st, ok := structs[name]
		if !ok {
			panic("missing type " + name)
		}
		fmt.Printf("message %s {\n", name)
		seen := map[int]bool{}
		for _, field := range st.Fields.List {
			if len(field.Names) != 1 || field.Tag == nil {
				panic("unsupported field in " + name)
			}
			raw, err := strconv.Unquote(field.Tag.Value)
			if err != nil {
				panic(err)
			}
			tag := reflect.StructTag(raw)
			jsonName := strings.Split(tag.Get("json"), ",")[0]
			number, err := strconv.Atoi(tag.Get("proto"))
			if err != nil || number <= 0 || seen[number] {
				panic("invalid proto tag: " + name + "." + field.Names[0].Name)
			}
			seen[number] = true
			typ, err := protoType(field.Type)
			if err != nil {
				panic(fmt.Sprintf("%s.%s: %v", name, field.Names[0].Name, err))
			}
			fmt.Printf("  %s %s = %d [json_name = %q];\n", typ, snake(jsonName), number, jsonName)
		}
		fmt.Print("}\n\n")
	}
}

func protoType(expr ast.Expr) (string, error) {
	switch x := expr.(type) {
	case *ast.StarExpr:
		return protoType(x.X)
	case *ast.ArrayType:
		t, err := protoType(x.Elt)
		if err != nil {
			return "", err
		}
		return "repeated " + t, nil
	case *ast.MapType:
		key, ok := x.Key.(*ast.Ident)
		if !ok || key.Name != "string" {
			return "", fmt.Errorf("map key must be string")
		}
		if v, ok := x.Value.(*ast.Ident); ok && v.Name == "ActionNames" {
			return "map<string, google.protobuf.ListValue>", nil
		}
		return "", fmt.Errorf("unsupported map")
	case *ast.Ident:
		switch x.Name {
		case "string":
			return "string", nil
		case "bool":
			return "bool", nil
		default:
			for _, name := range types {
				if x.Name == name {
					return name, nil
				}
			}
		}
	case *ast.SelectorExpr:
		pkg, ok := x.X.(*ast.Ident)
		if !ok {
			break
		}
		switch pkg.Name + "." + x.Sel.Name {
		case "types.UID":
			return "string", nil
		case "metav1.Time":
			return "google.protobuf.Timestamp", nil
		case "metav1.Condition":
			return "Condition", nil
		case "runtime.RawExtension":
			return "google.protobuf.Struct", nil
		}
	}
	return "", fmt.Errorf("unsupported Go type %T", expr)
}

func snake(s string) string {
	var b strings.Builder
	for i, r := range s {
		if unicode.IsUpper(r) {
			if i > 0 {
				b.WriteByte('_')
			}
			b.WriteRune(unicode.ToLower(r))
		} else {
			b.WriteRune(r)
		}
	}
	return b.String()
}
