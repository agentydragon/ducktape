#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

#include <cstdint>
#include <cstdlib>
#include <set>
#include <string>
#include <utility>

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

class ScopedEnv {
 public:
  ScopedEnv(std::string name, std::string value) : name_(std::move(name)) {
    const char* old_value = std::getenv(name_.c_str());
    if (old_value != nullptr) {
      had_old_value_ = true;
      old_value_ = old_value;
    }
    setenv(name_.c_str(), value.c_str(), /*overwrite=*/1);
  }

  ScopedEnv(const ScopedEnv&) = delete;
  ScopedEnv& operator=(const ScopedEnv&) = delete;

  ~ScopedEnv() {
    if (had_old_value_) {
      setenv(name_.c_str(), old_value_.c_str(), /*overwrite=*/1);
    } else {
      unsetenv(name_.c_str());
    }
  }

 private:
  std::string name_;
  bool had_old_value_ = false;
  std::string old_value_;
};

TEST(SelectorCpSatSolverTest, AllDifferentPropagatesBroadSpecificFixture) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "broad_owner" dense_domain { value_count: 2 } }
    variables { id: 1 debug_name: "strict_owner" dense_domain { value_count: 2 } }
    variables {
      id: 2
      debug_name: "reserved_owner"
      dense_domain { value_count: 3 }
    }

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
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 2 } }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_AMBIGUOUS);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  EXPECT_EQ(response.assignments_size(), 2);
}

TEST(SelectorCpSatSolverTest, UnprojectedVariablesDoNotCreateRows) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 1 } }
    variables {
      id: 1
      debug_name: "internal_ast_node"
      sparse_domain { values: [10, 11, 12, 13, 14] }
    }
    target_projections { target_id: 0 owner_variable_id: 0 }
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

TEST(SelectorCpSatSolverTest, SharedSparseDomainCanConstrainMultipleVariables) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    shared_sparse_domains {
      id: 4
      values: [1, 3]
    }
    variables {
      id: 0
      debug_name: "left"
      shared_sparse_domain_id: 4
    }
    variables {
      id: 1
      debug_name: "right"
      shared_sparse_domain_id: 4
    }
    allowed_tables {
      id: 0
      variable_ids: 0
      allowed_rows { values: 1 }
    }
    allowed_tables {
      id: 1
      variable_ids: 1
      allowed_rows { values: 3 }
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
  EXPECT_TRUE(RowHas(response.assignments(0), 0, 1));
  EXPECT_TRUE(RowHas(response.assignments(0), 1, 3));
}

TEST(SelectorCpSatSolverTest, MissingSharedSparseDomainIdIsInvalid) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables {
      id: 0
      debug_name: "owner"
      shared_sparse_domain_id: 99
    }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  EXPECT_NE(response.diagnostic().find("unknown shared_sparse_domain_id 99"),
            std::string::npos);
}

TEST(SelectorCpSatSolverTest, ConflictingTablesAreUnsat) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 2 } }
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

std::set<std::set<uint32_t>> ConflictSets(
    const cpsat::SelectorCpSatResponse& response) {
  std::set<std::set<uint32_t>> conflicts;
  for (const cpsat::ConflictSet& conflict : response.conflicts()) {
    conflicts.emplace(conflict.target_ids().begin(),
                      conflict.target_ids().end());
  }
  return conflicts;
}

// Targets 0 and 1 both need value 0 and targets 2 and 3 both need value 1,
// under one all_different; target 4 is independent of both clashes.
TEST(SelectorCpSatSolverTest, DisjointContradictionsAreSeparateConflictSets) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "a0" dense_domain { value_count: 3 } }
    variables { id: 1 debug_name: "a1" dense_domain { value_count: 3 } }
    variables { id: 2 debug_name: "b0" dense_domain { value_count: 3 } }
    variables { id: 3 debug_name: "b1" dense_domain { value_count: 3 } }
    variables { id: 4 debug_name: "free" dense_domain { value_count: 3 } }

    allowed_tables {
      id: 0
      variable_ids: 0
      allowed_rows { values: 0 }
      target_ids: 0
    }
    allowed_tables {
      id: 1
      variable_ids: 1
      allowed_rows { values: 0 }
      target_ids: 1
    }
    allowed_tables {
      id: 2
      variable_ids: 2
      allowed_rows { values: 1 }
      target_ids: 2
    }
    allowed_tables {
      id: 3
      variable_ids: 3
      allowed_rows { values: 1 }
      target_ids: 3
    }
    allowed_tables {
      id: 4
      variable_ids: 4
      allowed_rows { values: 0 }
      allowed_rows { values: 2 }
      target_ids: 4
    }

    all_different {
      id: 0
      variable_ids: [ 0, 1, 2, 3, 4 ]
      entry_targets { target_ids: 0 }
      entry_targets { target_ids: 1 }
      entry_targets { target_ids: 2 }
      entry_targets { target_ids: 3 }
      entry_targets { target_ids: 4 }
    }

    target_projections { target_id: 0 owner_variable_id: 0 }
    target_projections { target_id: 1 owner_variable_id: 1 }
    target_projections { target_id: 2 owner_variable_id: 2 }
    target_projections { target_id: 3 owner_variable_id: 3 }
    target_projections { target_id: 4 owner_variable_id: 4 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(ConflictSets(response),
            (std::set<std::set<uint32_t>>{{0, 1}, {2, 3}}));
  // With the conflicting targets disabled, value 0 is free again, so target 4
  // keeps both of its candidates.
  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_AMBIGUOUS);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  ASSERT_EQ(response.assignments_size(), 2);
  for (const cpsat::AssignmentRow& row : response.assignments()) {
    ASSERT_EQ(row.values_size(), 1);
    EXPECT_EQ(row.values(0).variable_id(), 4u);
  }
}

TEST(SelectorCpSatSolverTest, UnrelatedTargetResolvesBesideAConflict) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "a0" dense_domain { value_count: 2 } }
    variables { id: 1 debug_name: "a1" dense_domain { value_count: 2 } }
    variables { id: 2 debug_name: "free" dense_domain { value_count: 2 } }

    allowed_tables {
      id: 0
      variable_ids: 0
      allowed_rows { values: 0 }
      target_ids: 0
    }
    allowed_tables {
      id: 1
      variable_ids: 1
      allowed_rows { values: 0 }
      target_ids: 1
    }
    allowed_tables {
      id: 2
      variable_ids: 2
      allowed_rows { values: 1 }
      target_ids: 2
    }

    all_different {
      id: 0
      variable_ids: [ 0, 1, 2 ]
      entry_targets { target_ids: 0 }
      entry_targets { target_ids: 1 }
      entry_targets { target_ids: 2 }
    }

    target_projections { target_id: 0 owner_variable_id: 0 }
    target_projections { target_id: 1 owner_variable_id: 1 }
    target_projections { target_id: 2 owner_variable_id: 2 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(ConflictSets(response), (std::set<std::set<uint32_t>>{{0, 1}}));
  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  ASSERT_EQ(response.assignments_size(), 1);
  ASSERT_EQ(response.assignments(0).values_size(), 1);
  EXPECT_TRUE(RowHas(response.assignments(0), 2, 1));
}

// A constraint shared by two targets is enforced only while both are enabled,
// so its core names both.
TEST(SelectorCpSatSolverTest, SharedConstraintConflictNamesEveryAttributedTarget) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 2 } }
    variables { id: 1 debug_name: "anchor" dense_domain { value_count: 2 } }

    allowed_tables {
      id: 0
      variable_ids: [ 0, 1 ]
      allowed_rows { values: [ 0, 1 ] }
      target_ids: [ 0, 1 ]
    }
    allowed_tables {
      id: 1
      variable_ids: 1
      allowed_rows { values: 0 }
      target_ids: 1
    }

    target_projections { target_id: 0 owner_variable_id: 0 }
    target_projections { target_id: 1 owner_variable_id: 1 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(ConflictSets(response), (std::set<std::set<uint32_t>>{{0, 1}}));
  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  ASSERT_EQ(response.assignments_size(), 1);
  EXPECT_EQ(response.assignments(0).values_size(), 0);
}

TEST(SelectorCpSatSolverTest, SharedAllowedRowSetCanConstrainMultipleTables) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables {
      id: 0
      debug_name: "owner_a"
      dense_domain { value_count: 3 }
    }
    variables {
      id: 1
      debug_name: "owner_b"
      dense_domain { value_count: 3 }
    }

	    allowed_row_sets {
	      id: 7
	      arity: 1
	      values: 1
	    }

    allowed_tables { id: 0 variable_ids: 0 row_set_id: 7 }
    allowed_tables { id: 1 variable_ids: 1 row_set_id: 7 }

    target_projections { target_id: 0 owner_variable_id: 0 }
    target_projections { target_id: 1 owner_variable_id: 1 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
  ASSERT_EQ(response.assignments_size(), 1);
  EXPECT_TRUE(RowHas(response.assignments(0), 0, 1));
  EXPECT_TRUE(RowHas(response.assignments(0), 1, 1));
}

TEST(SelectorCpSatSolverTest, MissingAllowedRowSetIdIsInvalid) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables {
      id: 0
      debug_name: "owner"
      dense_domain { value_count: 2 }
    }
    allowed_tables { id: 3 variable_ids: 0 row_set_id: 99 }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  EXPECT_NE(response.diagnostic().find("unknown row_set_id 99"),
            std::string::npos);
}

TEST(SelectorCpSatSolverTest, SharedAllowedRowSetArityMismatchIsInvalid) {
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables {
      id: 0
      debug_name: "left"
      dense_domain { value_count: 2 }
    }
    variables {
      id: 1
      debug_name: "right"
      dense_domain { value_count: 2 }
    }
	    allowed_row_sets {
	      id: 5
	      arity: 1
	      values: 1
	    }
    allowed_tables { id: 4 variable_ids: 0 variable_ids: 1 row_set_id: 5 }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
	  EXPECT_NE(response.diagnostic().find("row_set_id 5 has arity 1"),
	            std::string::npos);
  EXPECT_NE(response.diagnostic().find("expected 2"), std::string::npos);
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
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 1 } }
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

TEST(SelectorCpSatSolverTest, NumSearchWorkersEnvAcceptsPositiveOverride) {
  ScopedEnv env("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS", "2");
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 1 } }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_SATISFIABLE);
  EXPECT_EQ(response.assignment_coverage(),
            cpsat::ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
}

TEST(SelectorCpSatSolverTest, InvalidNumSearchWorkersEnvIsInvalidResponse) {
  ScopedEnv env("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS", "0");
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 1 } }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  EXPECT_NE(response.diagnostic().find(
                "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS"),
            std::string::npos);
}

TEST(SelectorCpSatSolverTest, InvalidMaxTimeSecondsEnvIsInvalidResponse) {
  ScopedEnv env("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS", "not-time");
  const cpsat::SelectorCpSatRequest request = ParseRequest(R"pb(
    variables { id: 0 debug_name: "owner" dense_domain { value_count: 1 } }
    target_projections { target_id: 0 owner_variable_id: 0 }
  )pb");

  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);

  EXPECT_EQ(response.status(), cpsat::SOLVER_STATUS_INVALID);
  EXPECT_NE(response.diagnostic().find(
                "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS"),
            std::string::npos);
}

}  // namespace
