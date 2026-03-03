// Package nftables adapts Talos's NfTablesChainController and rule compiler
// for kubespand. The upstream sources are pulled at build time from a pinned
// Talos release (see MODULE.bazel http_file entries) and transformed via
// genrule sed into this package.
//
// Divergences from upstream Talos are marked with "DIVERGENCE:" comments.
package nftables

import (
	"context"
	"fmt"
	"slices"
	"strconv"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/google/nftables"
	"github.com/google/nftables/expr"
	"github.com/siderolabs/go-pointer"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"
)

// KubespandNfTablesChainController applies network.NfTablesChain COSI resources
// to the Linux nftables subsystem. Adapted from Talos's NfTablesChainController
// with kubespand-specific divergences.
//
// DIVERGENCE: This replaces the upstream NfTablesChainController.Run() with
// a version that:
//   - Skips preCreateIptablesNFTable (Talos-specific iptables compatibility)
//   - Uses flushWithEBUSYRetry instead of returning error to COSI runtime
//   - Uses "talos_kubespan" as the table name (not the Talos default "talos")
type KubespandNfTablesChainController struct {
	TableName string
}

// Name implements controller.Controller.
func (ctrl *KubespandNfTablesChainController) Name() string {
	return "network.NfTablesChainController"
}

// Inputs implements controller.Controller.
func (ctrl *KubespandNfTablesChainController) Inputs() []controller.Input {
	return []controller.Input{
		{
			Namespace: network.NamespaceName,
			Type:      network.NfTablesChainType,
			Kind:      controller.InputWeak,
		},
	}
}

// Outputs implements controller.Controller.
func (ctrl *KubespandNfTablesChainController) Outputs() []controller.Output {
	return nil
}

// Run implements controller.Controller.
//
//nolint:gocyclo,cyclop
func (ctrl *KubespandNfTablesChainController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	if ctrl.TableName == "" {
		ctrl.TableName = "talos_kubespan"
	}

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		// DIVERGENCE: No preCreateIptablesNFTable call. kubespand doesn't
		// need the iptables-nft mangle/KUBE-IPTABLES-HINT compatibility hack.

		var conn nftables.Conn

		list, err := safe.ReaderListAll[*network.NfTablesChain](ctx, r)
		if err != nil {
			return fmt.Errorf("error listing nftables chains: %w", err)
		}

		existingTables, err := conn.ListTablesOfFamily(nftables.TableFamilyINet)
		if err != nil {
			return fmt.Errorf("error listing existing nftables tables: %w", err)
		}

		var talosTable *nftables.Table

		if idx := slices.IndexFunc(existingTables, func(t *nftables.Table) bool { return t.Name == ctrl.TableName }); idx != -1 {
			talosTable = existingTables[idx]
		}

		if talosTable == nil {
			talosTable = &nftables.Table{
				Family: nftables.TableFamilyINet,
				Name:   ctrl.TableName,
			}

			conn.AddTable(talosTable)
		}

		existingChains, err := conn.ListChains()
		if err != nil {
			return fmt.Errorf("error listing existing nftables chains: %w", err)
		}

		for _, chain := range existingChains {
			if chain.Table.Name != ctrl.TableName {
				continue
			}

			conn.DelChain(chain)
		}

		setID := uint32(0)

		for chain := range list.All() {
			nfChain := conn.AddChain(&nftables.Chain{
				Name:     chain.Metadata().ID(),
				Table:    talosTable,
				Hooknum:  pointer.To(nftables.ChainHook(chain.TypedSpec().Hook)),
				Priority: pointer.To(nftables.ChainPriority(chain.TypedSpec().Priority)),
				Type:     nftables.ChainType(chain.TypedSpec().Type),
				Policy:   pointer.To(nftables.ChainPolicy(chain.TypedSpec().Policy)),
			})

			for _, rule := range chain.TypedSpec().Rules {
				compiled, err := NfTablesRule(&rule).Compile()
				if err != nil {
					return fmt.Errorf("error compiling nftables rule for chain %s: %w", nfChain.Name, err)
				}

				for _, compiledRule := range compiled.Rules {
					for i := range compiledRule {
						if lookup, ok := compiledRule[i].(*expr.Lookup); ok {
							if lookup.SetID >= uint32(len(compiled.Sets)) {
								return fmt.Errorf("invalid set ID %d in lookup", lookup.SetID)
							}

							set := compiled.Sets[lookup.SetID]
							setName := "__set" + strconv.Itoa(int(setID))

							if err = conn.AddSet(&nftables.Set{
								Table:     talosTable,
								ID:        setID,
								Name:      setName,
								Anonymous: true,
								Constant:  true,
								Interval:  set.IsInterval(),
								KeyType:   set.KeyType(),
							}, set.SetElements()); err != nil {
								return fmt.Errorf("error adding nftables set for chain %s: %w", nfChain.Name, err)
							}

							lookupOp := *lookup
							lookupOp.SetID = setID
							lookupOp.SetName = setName

							compiledRule[i] = &lookupOp

							setID++
						}
					}

					conn.AddRule(&nftables.Rule{
						Table: talosTable,
						Chain: nfChain,
						Exprs: compiledRule,
					})
				}
			}
		}

		// DIVERGENCE: Use EBUSY retry instead of returning error to COSI runtime.
		if err := flushWithEBUSYRetry(&conn, logger); err != nil {
			return fmt.Errorf("error flushing nftables: %w", err)
		}

		chainNames := safe.ToSlice(list, func(chain *network.NfTablesChain) string { return chain.Metadata().ID() })
		logger.Info("nftables chains updated", zap.Strings("chains", chainNames))

		r.ResetRestartBackoff()
	}
}
