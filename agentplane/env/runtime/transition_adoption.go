package runtime

import (
 "context"
 "errors"

 "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

type adoptionBarrierKind uint8
const (
 adoptionNativeStop adoptionBarrierKind = iota+1
 adoptionExecutionFence
 adoptionExecutionStart
 adoptionPublishDesired
 adoptionBootstrap
 adoptionAcknowledgement
 adoptionIncarnation
)
type adoptionBarrier struct {
 kind adoptionBarrierKind
 uid string
 sealUID string
 mode lifecycle.Mode
}
type adoptionPlan struct {
 targetUID string
 generation uint64
 sealed bool
 oldUID string
}
type adoptionOutcome uint8
const (
 adoptionNoSuccessor adoptionOutcome=iota
 adoptionRunningSuccessor
)

func (c *runtimeTransitionSequence) runAdoptionBarrier(ctx context.Context, step adoptionBarrier) (runtimeTransitionOutcome,error) {
 r:=c.r
 switch step.kind {
 case adoptionNativeStop:
  return runtimeTransitionOutcome{},r.waitForNativeStopped(ctx,c.adoption.generation)
 case adoptionExecutionFence:
  return runtimeTransitionOutcome{},r.waitForExecutionFenced(ctx,c.adoption.generation)
 case adoptionExecutionStart:
  return runtimeTransitionOutcome{},r.startExecutionForUID(ctx,step.uid)
 case adoptionPublishDesired:
  generation,err:=r.publishDesired(ctx,step.mode,step.uid,step.sealUID)
  return runtimeTransitionOutcome{generation:generation},err
 case adoptionBootstrap:
  return runtimeTransitionOutcome{},r.requestBootstrap(ctx)
 case adoptionAcknowledgement:
  return runtimeTransitionOutcome{},r.waitSessionStage(ctx,lifecycle.Quiesced)
 case adoptionIncarnation:
  if err:=ctx.Err();err!=nil{return runtimeTransitionOutcome{},err}
  if err:=r.state.AdoptIncarnation(c.adoption.targetUID);err!=nil{return runtimeTransitionOutcome{},err}
  if c.adoption.sealed&&c.adoption.oldUID==c.adoption.targetUID{return runtimeTransitionOutcome{adoption:adoptionNoSuccessor},nil}
  return runtimeTransitionOutcome{adoption:adoptionRunningSuccessor},nil
 default:
  return runtimeTransitionOutcome{},errors.New("unknown adoption barrier")
 }
}

func (c *runtimeTransitionSequence) recordAdoptionOutcome(step runtimeTransitionStep,outcome runtimeTransitionOutcome) error {
 if outcome.generation!=0 {c.adoption.generation=outcome.generation}
 if step.adoption.kind==adoptionIncarnation&&outcome.adoption==adoptionRunningSuccessor {
  uid:=c.adoption.targetUID
  c.steps=append(c.steps,
   runtimeTransitionStep{stage:lifecycle.RuntimeTransitionExecutionStarting,adoption:adoptionBarrier{kind:adoptionExecutionStart,uid:uid}},
   runtimeTransitionStep{stage:lifecycle.RuntimeTransitionIntentPublishing,adoption:adoptionBarrier{kind:adoptionPublishDesired,uid:uid,mode:lifecycle.Running}},
   runtimeTransitionStep{stage:lifecycle.RuntimeTransitionBootstrapRequesting,adoption:adoptionBarrier{kind:adoptionBootstrap}},
  )
 }
 return nil
}

func (c *runtimeTransitionSequence) planAdoption(ctx context.Context,uid string)(adoptionPlan,[]runtimeTransitionStep,error){
 r:=c.r
 r.mu.Lock()
 if r.closing||r.failed||!r.startupComplete {r.mu.Unlock();return adoptionPlan{},nil,status.Error(codes.Unavailable,"environment Runtime is unavailable")}
 desired:=r.desired
 accepted:=r.admissionOpen
 r.mu.Unlock()
 sealedUID,sealed,err:=r.state.SealedUID()
 if err!=nil{return adoptionPlan{},nil,err}
 oldUID,known:=r.state.IncarnationUID()
 if sealed {oldUID,known=sealedUID,true}
 plan:=adoptionPlan{targetUID:uid,generation:desired.Generation,sealed:sealed,oldUID:oldUID}
 if known&&oldUID==uid&&!sealed&&desired.Mode==lifecycle.Running {
  if !accepted{return plan,nil,status.Error(codes.Unavailable,"environment revision has not completed preparation")}
  return plan,[]runtimeTransitionStep{{stage:lifecycle.RuntimeTransitionBootstrapRequesting,adoption:adoptionBarrier{kind:adoptionBootstrap}}},nil
 }
 var steps []runtimeTransitionStep
 add:=func(stage lifecycle.RuntimeTransitionStage,barrier adoptionBarrier){steps=append(steps,runtimeTransitionStep{stage:stage,adoption:barrier})}
 if desired.Mode==lifecycle.Inactive {
  if err:=r.waitForNativeStopped(ctx,desired.Generation);err!=nil{return plan,nil,err}
 }
 if known {
  seal:=""
  if sealed {seal=oldUID}
  if desired.Mode!=lifecycle.Quiescing||desired.IncarnationUID!=oldUID||desired.SealUID!=seal {
   if sealed {add(lifecycle.RuntimeTransitionExecutionStarting,adoptionBarrier{kind:adoptionExecutionStart,uid:oldUID})}
   add(lifecycle.RuntimeTransitionIntentPublishing,adoptionBarrier{kind:adoptionPublishDesired,uid:oldUID,sealUID:seal,mode:lifecycle.Quiescing})
  }
  add(lifecycle.RuntimeTransitionNativeStopping,adoptionBarrier{kind:adoptionNativeStop})
  add(lifecycle.RuntimeTransitionExecutionFencing,adoptionBarrier{kind:adoptionExecutionFence})
  if sealed {add(lifecycle.RuntimeTransitionHostAcknowledging,adoptionBarrier{kind:adoptionAcknowledgement})}
 }
 add(lifecycle.RuntimeTransitionIncarnationAdopting,adoptionBarrier{kind:adoptionIncarnation})
 return plan,steps,nil
}
