#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

#include <algorithm>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model.pb.h"
#include "ortools/sat/cp_model_solver.h"
#include "ortools/sat/model.h"

namespace ducktape::debundle::solver_backends::ortools_cpsat {
namespace {

namespace sat = ::operations_research::sat;
using VariableMap = std::map<uint32_t, sat::IntVar>;

struct ProjectionRow {
  std::vector<std::pair<uint32_t, int64_t>> values;

  bool operator<(const ProjectionRow& other) const {
    return values < other.values;
  }
};

SelectorCpSatResponse InvalidResponse(const absl::Status& status) {
  SelectorCpSatResponse response;
  response.set_status(SOLVER_STATUS_INVALID);
  response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
  response.set_diagnostic(std::string(status.message()));
  return response;
}

absl::Status MissingVariableStatus(uint32_t variable_id) {
  return absl::InvalidArgumentError(
      absl::StrCat("unknown variable id ", variable_id));
}

absl::Status AddVariables(const SelectorCpSatRequest& request,
                          sat::CpModelBuilder* model,
                          VariableMap* variables) {
  for (const Variable& variable : request.variables()) {
    if (variable.values().empty()) {
      return absl::InvalidArgumentError(
          absl::StrCat("variable ", variable.id(), " has an empty domain"));
    }
    if (variables->contains(variable.id())) {
      return absl::InvalidArgumentError(
          absl::StrCat("duplicate variable id ", variable.id()));
    }

    std::vector<int64_t> values(variable.values().begin(),
                                variable.values().end());
    std::sort(values.begin(), values.end());
    if (std::adjacent_find(values.begin(), values.end()) != values.end()) {
      return absl::InvalidArgumentError(absl::StrCat(
          "variable ", variable.id(), " has duplicate domain values"));
    }

    sat::IntVar int_var =
        model->NewIntVar(::operations_research::Domain::FromValues(values))
            .WithName(variable.debug_name());
    variables->emplace(variable.id(), int_var);
  }
  return absl::OkStatus();
}

absl::StatusOr<std::vector<sat::IntVar>> LookupVariables(
    const VariableMap& variables,
    const google::protobuf::RepeatedField<uint32_t>& ids) {
  std::vector<sat::IntVar> found;
  found.reserve(ids.size());
  for (uint32_t id : ids) {
    const auto it = variables.find(id);
    if (it == variables.end()) {
      return MissingVariableStatus(id);
    }
    found.push_back(it->second);
  }
  return found;
}

absl::Status AddAllowedTables(const SelectorCpSatRequest& request,
                              const VariableMap& variables,
                              sat::CpModelBuilder* model) {
  for (const TableConstraint& table : request.allowed_tables()) {
    absl::StatusOr<std::vector<sat::IntVar>> table_variables =
        LookupVariables(variables, table.variable_ids());
    if (!table_variables.ok()) {
      return table_variables.status();
    }
    if (table_variables->empty()) {
      return absl::InvalidArgumentError(absl::StrCat(
          "table constraint ", table.id(), " has no variables"));
    }

    sat::TableConstraint allowed =
        model->AddAllowedAssignments(*table_variables);
    for (int row_index = 0; row_index < table.allowed_rows_size();
         ++row_index) {
      const Tuple& row = table.allowed_rows(row_index);
      if (static_cast<size_t>(row.values_size()) != table_variables->size()) {
        return absl::InvalidArgumentError(absl::StrCat(
            "table constraint ", table.id(), " row ", row_index,
            " has arity ", row.values_size(), ", expected ",
            table_variables->size()));
      }
      const std::vector<int64_t> values(row.values().begin(),
                                        row.values().end());
      allowed.AddTuple(values);
    }
  }
  return absl::OkStatus();
}

absl::Status AddBinaryConstraints(const SelectorCpSatRequest& request,
                                  const VariableMap& variables,
                                  sat::CpModelBuilder* model) {
  for (const BinaryConstraint& constraint : request.binary_constraints()) {
    const auto left = variables.find(constraint.left_variable_id());
    if (left == variables.end()) {
      return MissingVariableStatus(constraint.left_variable_id());
    }
    const auto right = variables.find(constraint.right_variable_id());
    if (right == variables.end()) {
      return MissingVariableStatus(constraint.right_variable_id());
    }

    switch (constraint.kind()) {
      case BINARY_CONSTRAINT_KIND_EQUAL:
        model->AddEquality(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_NOT_EQUAL:
        model->AddNotEqual(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_ORDINAL_BEFORE:
        model->AddLessThan(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_UNSPECIFIED:
      default:
        return absl::InvalidArgumentError(absl::StrCat(
            "unsupported binary constraint kind ",
            static_cast<int>(constraint.kind())));
    }
  }
  return absl::OkStatus();
}

absl::Status AddAllDifferentConstraints(const SelectorCpSatRequest& request,
                                        const VariableMap& variables,
                                        sat::CpModelBuilder* model) {
  for (const AllDifferent& all_different : request.all_different()) {
    absl::StatusOr<std::vector<sat::IntVar>> all_different_variables =
        LookupVariables(variables, all_different.variable_ids());
    if (!all_different_variables.ok()) {
      return all_different_variables.status();
    }
    if (all_different_variables->size() < 2) {
      return absl::InvalidArgumentError(absl::StrCat(
          "all_different constraint ", all_different.id(),
          " has fewer than two variables"));
    }
    model->AddAllDifferent(*all_different_variables);
  }
  return absl::OkStatus();
}

absl::StatusOr<std::vector<uint32_t>> ProjectionVariableIds(
    const SelectorCpSatRequest& request, const VariableMap& variables) {
  std::set<uint32_t> ids;
  for (const TargetProjection& projection : request.target_projections()) {
    ids.insert(projection.owner_variable_id());
    if (projection.has_binding_variable()) {
      ids.insert(projection.binding_variable_id());
    }
  }
  for (uint32_t id : ids) {
    if (!variables.contains(id)) {
      return MissingVariableStatus(id);
    }
  }
  return std::vector<uint32_t>(ids.begin(), ids.end());
}

SelectorCpSatResponse ResponseFromSolver(
    const sat::CpSolverResponse& solver_response,
    const std::set<ProjectionRow>& projection_rows, bool complete) {
  SelectorCpSatResponse response;

  switch (solver_response.status()) {
    case sat::CpSolverStatus::INFEASIBLE:
      response.set_status(SOLVER_STATUS_UNSATISFIABLE);
      response.set_assignment_coverage(
          ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
      return response;
    case sat::CpSolverStatus::OPTIMAL:
    case sat::CpSolverStatus::FEASIBLE:
      break;
    case sat::CpSolverStatus::MODEL_INVALID:
      response.set_status(SOLVER_STATUS_INVALID);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic(sat::CpSolverResponseStats(solver_response));
      return response;
    case sat::CpSolverStatus::UNKNOWN:
    default:
      response.set_status(SOLVER_STATUS_UNKNOWN);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic(sat::CpSolverResponseStats(solver_response));
      return response;
  }

  response.set_status(projection_rows.size() == 1 ? SOLVER_STATUS_SATISFIABLE
                                                  : SOLVER_STATUS_AMBIGUOUS);
  response.set_assignment_coverage(
      complete ? ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE
               : ASSIGNMENT_COVERAGE_SAMPLE);
  if (!complete) {
    response.set_diagnostic(sat::CpSolverResponseStats(solver_response));
  }
  for (const ProjectionRow& row : projection_rows) {
    AssignmentRow* assignment_row = response.add_assignments();
    for (const auto& [variable_id, value] : row.values) {
      Assignment* assignment = assignment_row->add_values();
      assignment->set_variable_id(variable_id);
      assignment->set_value(value);
    }
  }
  return response;
}

absl::Status BuildCpModel(const SelectorCpSatRequest& request,
                          sat::CpModelBuilder* model,
                          VariableMap* variables) {
  if (const absl::Status status = AddVariables(request, model, variables);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status = AddAllowedTables(request, *variables, model);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status =
          AddBinaryConstraints(request, *variables, model);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status =
          AddAllDifferentConstraints(request, *variables, model);
      !status.ok()) {
    return status;
  }
  return absl::OkStatus();
}

}  // namespace

SelectorCpSatResponse SolveSelectorCpSat(const SelectorCpSatRequest& request) {
  sat::CpModelBuilder model;
  VariableMap variables;
  const absl::Status build_status =
      BuildCpModel(request, &model, &variables);
  if (!build_status.ok()) {
    return InvalidResponse(build_status);
  }

  absl::StatusOr<std::vector<uint32_t>> projection_variable_ids =
      ProjectionVariableIds(request, variables);
  if (!projection_variable_ids.ok()) {
    return InvalidResponse(projection_variable_ids.status());
  }

  std::set<ProjectionRow> projection_rows;
  sat::Model solver_model;
  sat::SatParameters parameters;
  parameters.set_enumerate_all_solutions(true);
  parameters.set_num_search_workers(1);
  solver_model.Add(sat::NewSatParameters(parameters));
  solver_model.Add(sat::NewFeasibleSolutionObserver(
      [&](const sat::CpSolverResponse& response) {
        ProjectionRow row;
        row.values.reserve(projection_variable_ids->size());
        for (uint32_t variable_id : *projection_variable_ids) {
          row.values.push_back(
              {variable_id,
               sat::SolutionIntegerValue(response, variables.at(variable_id))});
        }
        projection_rows.insert(std::move(row));
      }));

  const sat::CpSolverResponse solver_response =
      sat::SolveCpModel(model.Build(), &solver_model);
  const bool complete = solver_response.status() == sat::CpSolverStatus::OPTIMAL;
  return ResponseFromSolver(solver_response, projection_rows, complete);
}

}  // namespace ducktape::debundle::solver_backends::ortools_cpsat
