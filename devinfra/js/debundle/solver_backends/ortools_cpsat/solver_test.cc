#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

#include "absl/log/check.h"
#include "google/protobuf/text_format.h"

namespace cpsat = ducktape::debundle::solver_backends::ortools_cpsat;

namespace {

cpsat::SelectorCpSatRequest ParseRequestOrDie(const char* textproto) {
  cpsat::SelectorCpSatRequest request;
  CHECK(google::protobuf::TextFormat::ParseFromString(textproto, &request))
      << textproto;
  return request;
}

bool RowHas(const cpsat::AssignmentRow& row, uint32_t variable_id,
            int64_t value) {
  for (const cpsat::Assignment& assignment : row.values()) {
    if (assignment.variable_id() == variable_id &&
        assignment.value() == value) {
      return true;
    }
  }
  return false;
}

void AllDifferentPropagatesBroadSpecificFixture() {
  const cpsat::SelectorCpSatRequest request = ParseRequestOrDie(R"pb(
    variables { id: 0 values: 0 values: 1 debug_name: "broad_owner" }
    variables { id: 1 values: 0 values: 1 debug_name: "strict_owner" }
    variables { id: 2 values: 0 values: 1 values: 2 debug_name: "reserved_owner" }

    all_different { id: 0 variable_ids: 0 variable_ids: 1 variable_ids: 2 }

    allowed_tables {
      id: 0
      variable_ids: 0
      allowed_rows { values: 0 }
      allowed_rows { values: 1 }
    }
    allowed_tables {
      id: 1
      variable_ids: 1
      allowed_rows { values: 1 }
    }
    allowed_tables {
      id: 2
      variable_ids: 2
      allowed_rows { values: 2 }
    }

    target_projections { target_id: 0 owner_variable_id: 0 }
    target_projections { target_id: 1 owner_variable_id: 1 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  CHECK_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  CHECK_EQ(response.assignment_coverage(),
           cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  CHECK_EQ(response.assignments_size(), 1);
  CHECK(RowHas(response.assignments(0), 0, 0));
  CHECK(RowHas(response.assignments(0), 1, 1));
}

void MultipleProjectionRowsAreAmbiguous() {
  const cpsat::SelectorCpSatRequest request = ParseRequestOrDie(R"pb(
    variables { id: 0 values: 0 values: 1 debug_name: "owner" }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  CHECK_EQ(response.status(), cpsat::SOLVER_STATUS_AMBIGUOUS);
  CHECK_EQ(response.assignment_coverage(),
           cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  CHECK_EQ(response.assignments_size(), 2);
}

void ConflictingTablesAreUnsat() {
  const cpsat::SelectorCpSatRequest request = ParseRequestOrDie(R"pb(
    variables { id: 0 values: 0 values: 1 debug_name: "owner" }
    allowed_tables {
      id: 0
      variable_ids: 0
      allowed_rows { values: 0 }
    }
    allowed_tables {
      id: 1
      variable_ids: 0
      allowed_rows { values: 1 }
    }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  CHECK_EQ(response.status(), cpsat::SOLVER_STATUS_UNSATISFIABLE);
  CHECK_EQ(response.assignment_coverage(),
           cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  CHECK_EQ(response.assignments_size(), 0);
}

void InvalidProblemReportsDiagnostic() {
  const cpsat::SelectorCpSatRequest request = ParseRequestOrDie(R"pb(
    variables { id: 0 debug_name: "owner" }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  CHECK_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  CHECK(!response.diagnostic().empty());
}

}  // namespace

int main() {
  AllDifferentPropagatesBroadSpecificFixture();
  MultipleProjectionRowsAreAmbiguous();
  ConflictingTablesAreUnsat();
  InvalidProblemReportsDiagnostic();
  return 0;
}
