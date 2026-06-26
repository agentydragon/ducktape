#include "absl/log/check.h"
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model.pb.h"
#include "ortools/sat/cp_model_solver.h"

namespace sat = operations_research::sat;

namespace {

bool IsSolved(const sat::CpSolverResponse& response) {
  return response.status() == sat::CpSolverStatus::OPTIMAL ||
         response.status() == sat::CpSolverStatus::FEASIBLE;
}

}  // namespace

int main() {
  sat::CpModelBuilder model;
  const operations_research::Domain candidate_domain(0, 2);
  const sat::IntVar broad_owner =
      model.NewIntVar(candidate_domain).WithName("broad_owner");
  const sat::IntVar strict_owner =
      model.NewIntVar(candidate_domain).WithName("strict_owner");
  const sat::IntVar reserved_owner =
      model.NewIntVar(candidate_domain).WithName("reserved_owner");

  model.AddAllDifferent({broad_owner, strict_owner, reserved_owner});

  sat::TableConstraint broad_candidates =
      model.AddAllowedAssignments({broad_owner});
  broad_candidates.AddTuple({0});
  broad_candidates.AddTuple({1});

  sat::TableConstraint strict_candidates =
      model.AddAllowedAssignments({strict_owner});
  strict_candidates.AddTuple({1});

  model.AddEquality(reserved_owner, 2);

  const sat::CpSolverResponse response = sat::Solve(model.Build());
  CHECK(IsSolved(response)) << "CP-SAT did not find a feasible solution:\n"
                            << sat::CpSolverResponseStats(response);

  CHECK_EQ(sat::SolutionIntegerValue(response, strict_owner), 1)
      << "strict owner should be fixed by its singleton table";
  CHECK_EQ(sat::SolutionIntegerValue(response, reserved_owner), 2)
      << "reserved owner should be fixed by equality";
  CHECK_EQ(sat::SolutionIntegerValue(response, broad_owner), 0)
      << "broad owner should be forced by all_different propagation";

  return 0;
}
