package ginkgospike_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/system_tests/x/go_runners/driver"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var (
	owner  *driver.Host
	shared driver.Handle
)

func TestSharedStack(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Shared k3s stack runner spike")
}

var _ = SynchronizedBeforeSuite(func(ctx SpecContext) []byte {
	var err error
	owner, err = driver.Start(ctx)
	Expect(err).NotTo(HaveOccurred())
	encoded, err := json.Marshal(owner.Handle)
	Expect(err).NotTo(HaveOccurred())
	return encoded
}, func(encoded []byte) {
	// Workers receive only connection information; process 1 retains the stack's ownership.
	Expect(json.Unmarshal(encoded, &shared)).To(Succeed())
}, NodeTimeout(15*time.Minute))

var _ = SynchronizedAfterSuite(func() {}, func(ctx SpecContext) {
	if owner != nil {
		defer func() { Expect(owner.Stop(ctx)).To(Succeed()) }()
		Expect(shared.Verify(ctx)).To(Succeed())
	}
}, NodeTimeout(5*time.Minute))

var _ = Describe("independent scenarios", func() {
	for _, name := range driver.Cases() {
		It(name, func(ctx SpecContext) {
			Expect(shared.Begin(ctx, name)).To(Succeed())
			DeferCleanup(func(ctx context.Context) {
				Expect(shared.Cleanup(ctx, name)).To(Succeed())
			}, NodeTimeout(5*time.Minute))
			Expect(shared.Run(ctx, name)).To(Succeed())
		}, SpecTimeout(driver.CaseTimeout(name)))
	}
})

var _ = It("checks cleanup with exclusive access", Serial, func(ctx SpecContext) {
	Expect(shared.Verify(ctx)).To(Succeed())
}, SpecTimeout(time.Minute))
