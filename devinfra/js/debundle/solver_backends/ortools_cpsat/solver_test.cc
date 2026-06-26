#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

#include "google/protobuf/text_format.h"
#include "gtest/gtest.h"

namespace cpsat = ducktape::debundle::solver_backends::ortools_cpsat;

namespace {

cpsat::SelectorCpSatRequest ParseRequest(const char* textproto) {
  cpsat::SelectorCpSatRequest request;
  EXPECT_TRUE(google::protobuf::TextFormat::ParseFromString(textproto, &request))
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

TEST(SelectorCpSatSolverTest, AllDifferentPropagatesBroadSpecificFixture) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
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

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  ASSERT_EQ(response.assignments_size(), 1);
  EXPECT_TRUE(RowHas(response.assignments(0), 0, 0));
  EXPECT_TRUE(RowHas(response.assignments(0), 1, 1));
}

TEST(SelectorCpSatSolverTest, MultipleProjectionRowsAreAmbiguous) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 values: 0 values: 1 debug_name: "owner" }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_AMBIGUOUS);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  EXPECT_EQ(response.assignments_size(), 2);
}

TEST(SelectorCpSatSolverTest, ConflictingTablesAreUnsat) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
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

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_UNSATISFIABLE);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  EXPECT_EQ(response.assignments_size(), 0);
}

TEST(SelectorCpSatSolverTest, InvalidProblemReportsDiagnostic) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  EXPECT_FALSE(response.diagnostic().empty());
}

TEST(SelectorCpSatSolverTest, ConstantBindingProjectionDoesNotAddVariable) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 values: 0 debug_name: "owner" }
    target_projections {
      target_id: 0
      owner_variable_id: 0
      binding_const: "minA"
    }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  ASSERT_EQ(response.assignments_size(), 1);
  EXPECT_TRUE(RowHas(response.assignments(0), 0, 0));
  EXPECT_EQ(response.assignments(0).values_size(), 1);
}

}  // namespace
