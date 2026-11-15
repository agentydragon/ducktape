//! Explicit saved projections for report records with artifact-only spans.

use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use swc_common::Span;

use crate::execution_evidence::ExecutionPrecisionLoss;

use super::schema::{
    LocalRecordFactoryRejection, LocalRecordFactoryUse, NativeOperationInputUse,
    ReentryFreeOperationUse,
};

#[derive(Serialize, Deserialize)]
pub struct SavedNativeOperationInputUse<'a> {
    report: Cow<'a, NativeOperationInputUse>,
    span: Span,
}

impl<'a> SavedNativeOperationInputUse<'a> {
    pub fn snapshot(report: &'a NativeOperationInputUse) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
        }
    }

    pub fn restore(self) -> NativeOperationInputUse {
        let mut report = self.report.into_owned();
        report.span = self.span;
        report
    }
}

#[derive(Serialize, Deserialize)]
pub struct SavedLocalRecordFactoryUse<'a> {
    report: Cow<'a, LocalRecordFactoryUse>,
    span: Span,
}

impl<'a> SavedLocalRecordFactoryUse<'a> {
    pub fn snapshot(report: &'a LocalRecordFactoryUse) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
        }
    }

    pub fn restore(self) -> LocalRecordFactoryUse {
        let mut report = self.report.into_owned();
        report.span = self.span;
        report
    }
}

#[derive(Serialize, Deserialize)]
pub struct SavedLocalRecordFactoryRejection<'a> {
    report: Cow<'a, LocalRecordFactoryRejection>,
    span: Span,
}

impl<'a> SavedLocalRecordFactoryRejection<'a> {
    pub fn snapshot(report: &'a LocalRecordFactoryRejection) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
        }
    }

    pub fn restore(self) -> LocalRecordFactoryRejection {
        let mut report = self.report.into_owned();
        report.span = self.span;
        report
    }
}

#[derive(Serialize, Deserialize)]
pub struct SavedReentryFreeOperationUse<'a> {
    report: Cow<'a, ReentryFreeOperationUse>,
    span: Span,
}

impl<'a> SavedReentryFreeOperationUse<'a> {
    pub fn snapshot(report: &'a ReentryFreeOperationUse) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
        }
    }

    pub fn restore(self) -> ReentryFreeOperationUse {
        let mut report = self.report.into_owned();
        report.span = self.span;
        report
    }
}

#[derive(Serialize, Deserialize)]
pub struct SavedExecutionPrecisionLoss<'a> {
    report: Cow<'a, ExecutionPrecisionLoss>,
    span: Span,
}

impl<'a> SavedExecutionPrecisionLoss<'a> {
    pub fn snapshot(report: &'a ExecutionPrecisionLoss) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
        }
    }

    pub fn restore(self) -> ExecutionPrecisionLoss {
        let mut report = self.report.into_owned();
        report.span = self.span;
        report
    }
}
