package runtime

import (
 "context"
 "errors"

 "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

type quiesceBarrierKind uint8
const quiesceSeal quiesceBarrierKind = 1

type quiescePlan struct {
 request lifecycle.RuntimeTransitionRequest
 factsHaveIdleSince bool
 factsIdleSinceMicros int64
}

func(c *runtimeTransitionSequence)planQuiesce(request lifecycle.RuntimeTransitionRequest)(quiescePlan,[]runtimeTransitionStep,error){
 facts,err:=c.r.facts()
 if err!=nil{return quiescePlan{},nil,err}
 plan:=quiescePlan{request:request}
 if facts.IdleSince!=nil {plan.factsHaveIdleSince=true;plan.factsIdleSinceMicros=facts.IdleSince.AsTime().UnixMicro()}
 return plan,[]runtimeTransitionStep{{stage:lifecycle.RuntimeTransitionQuiesceSealing,quiesce:quiesceSeal}},nil
}

func(c *runtimeTransitionSequence)runQuiesceBarrier(ctx context.Context,kind quiesceBarrierKind)(runtimeTransitionOutcome,error){
 r:=c.r
 if kind!=quiesceSeal{return runtimeTransitionOutcome{},errors.New("unknown quiesce barrier")}
 r.mu.Lock()
 if r.closing||r.failed||!r.startupComplete {r.mu.Unlock();return runtimeTransitionOutcome{},status.Error(codes.Unavailable,"environment Runtime is unavailable")}
 plan:=c.quiesce
 if !r.state.Lameduck()&&plan.request.HasIdleSince&&(!plan.factsHaveIdleSince||plan.factsIdleSinceMicros!=plan.request.IdleSinceMicros||r.activeAdmissions!=0||r.activeExecutionCalls!=0||r.executionOwner.recovering()||r.idleSince==nil||r.idleSince.AsTime().UnixMicro()!=plan.request.IdleSinceMicros){
  r.mu.Unlock();return runtimeTransitionOutcome{},status.Error(codes.Aborted,"the Sandbox is busy or its idle_since differs")
 }
 r.admissionOpen=false
 if err:=r.state.EnterLameduck();err!=nil {r.mu.Unlock();r.fail(err);return runtimeTransitionOutcome{},err}
 uid,err:=r.state.RequireIncarnationUID()
 if err!=nil {r.mu.Unlock();return runtimeTransitionOutcome{},err}
 current:=r.desired
 r.mu.Unlock()
 if current.Mode==lifecycle.Quiescing&&current.SealUID==uid {
  progress,found,err:=r.readSessionStatus(ctx)
  if err!=nil{return runtimeTransitionOutcome{},err}
  if !found||progress.Generation!=current.Generation||progress.Error=="" {return runtimeTransitionOutcome{},nil}
 }
 _,err=r.publishDesired(ctx,lifecycle.Quiescing,uid,uid)
 return runtimeTransitionOutcome{},err
}
