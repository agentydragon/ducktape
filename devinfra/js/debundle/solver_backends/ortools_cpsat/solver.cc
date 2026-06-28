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
#include "ortools/sat/cp_model_checker.h"
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

struct ProjectionVariable {
  uint32_t id;
  sat::IntVar variable;
};

SelectorCpSatResponse InvalidResponse(const absl::Status& status) {
  SelectorCpSatResponse response;
  response.set_status(SOLVER_STATUS_INVALID);
  response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
  response.set_diagnostic(std::string(status.message()));
  return response;
}

SelectorCpSatResponse InvalidModelResponse(
    const sat::CpSolverResponse& solver_response,
    const sat::CpModelProto& model_proto) {
  SelectorCpSatResponse response;
  response.set_status(SOLVER_STATUS_INVALID);
  response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
  response.set_solver_response_stats(sat::CpSolverResponseStats(solver_response));
  const std::string validation_error = sat::ValidateCpModel(model_proto);
  if (validation_error.empty()) {
    response.set_diagnostic(
        "CP-SAT reported MODEL_INVALID, but ValidateCpModel returned no error");
  } else {
    response.set_diagnostic(validation_error);
  }
  return response;
}

absl::Status MissingVariableStatus(uint32_t variable_id) {
  return absl::InvalidArgumentError(
      absl::StrCat("unknown variable id ", variable_id));
}

absl::StatusOr<::operations_research::Domain> DomainForVariable(
    const Variable& variable) {
  switch (variable.domain_case()) {
    case Variable::kDenseDomain: {
      if (variable.dense_domain().value_count() == 0) {
        return absl::InvalidArgumentError(
            absl::StrCat("variable ", variable.id(), " has an empty domain"));
      }
      return ::operations_research::Domain(
          0, static_cast<int64_t>(variable.dense_domain().value_count()) - 1);
    }
    case Variable::kSparseDomain: {
      if (variable.sparse_domain().values().empty()) {
        return absl::InvalidArgumentError(
            absl::StrCat("variable ", variable.id(), " has an empty domain"));
      }
      std::vector<int64_t> values(variable.sparse_domain().values().begin(),
                                  variable.sparse_domain().values().end());
      std::sort(values.begin(), values.end());
      if (std::adjacent_find(values.begin(), values.end()) != values.end()) {
        return absl::InvalidArgumentError(absl::StrCat(
            "variable ", variable.id(), " has duplicate domain values"));
      }
      return ::operations_research::Domain::FromValues(values);
    }
    case Variable::DOMAIN_NOT_SET:
      return absl::InvalidArgumentError(
          absl::StrCat("variable ", variable.id(), " has no domain"));
  }
  return absl::InvalidArgumentError(
      absl::StrCat("variable ", variable.id(), " has unsupported domain"));
}

absl::Status AddVariables(const SelectorCpSatRequest& request,
                          sat::CpModelBuilder* model,
                          VariableMap* variables) {
  for (const Variable& variable : request.variables()) {
    if (variables->contains(variable.id())) {
      return absl::InvalidArgumentError(
          absl::StrCat("duplicate variable id ", variable.id()));
    }

    absl::StatusOr<::operations_research::Domain> domain =
        DomainForVariable(variable);
    if (!domain.ok()) {
      return domain.status();
    }

    sat::IntVar int_var =
        model->NewIntVar(*domain).WithName(variable.debug_name());
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

absl::Status AddLinearConstraints(const SelectorCpSatRequest& request,
                                  const VariableMap& variables,
                                  sat::CpModelBuilder* model) {
  for (const LinearConstraint& constraint : request.linear_constraints()) {
    if (constraint.variable_ids_size() == 0) {
      return absl::InvalidArgumentError("linear constraint has no variables");
    }
    if (constraint.variable_ids_size() != constraint.coefficients_size()) {
      return absl::InvalidArgumentError(absl::StrCat(
          "linear constraint has ", constraint.variable_ids_size(),
          " variables but ", constraint.coefficients_size(), " coefficients"));
    }
    if (constraint.domain_size() == 0 || constraint.domain_size() % 2 != 0) {
      return absl::InvalidArgumentError(
          "linear constraint has invalid flat interval domain");
    }
    for (int index = 0; index < constraint.domain_size(); index += 2) {
      if (constraint.domain(index) > constraint.domain(index + 1)) {
        return absl::InvalidArgumentError(
            "linear constraint has inverted domain interval");
      }
    }
    std::vector<sat::IntVar> linear_variables;
    linear_variables.reserve(constraint.variable_ids_size());
    for (uint32_t variable_id : constraint.variable_ids()) {
      const auto variable = variables.find(variable_id);
      if (variable == variables.end()) {
        return MissingVariableStatus(variable_id);
      }
      linear_variables.push_back(variable->second);
    }
    const std::vector<int64_t> coefficients(constraint.coefficients().begin(),
                                            constraint.coefficients().end());
    const std::vector<int64_t> domain(constraint.domain().begin(),
                                      constraint.domain().end());
    const sat::LinearExpr expression =
        sat::LinearExpr::WeightedSum(linear_variables, coefficients) +
        constraint.offset();
    model->AddLinearConstraint(expression,
                               ::operations_research::Domain::FromFlatIntervals(
                                   domain));
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

absl::StatusOr<std::vector<ProjectionVariable>> ProjectionVariables(
    const SelectorCpSatRequest& request, const VariableMap& variables) {
  std::set<uint32_t> ids;
  for (const TargetProjection& projection : request.target_projections()) {
    ids.insert(projection.owner_variable_id());
    if (projection.has_binding_variable_id()) {
      ids.insert(projection.binding_variable_id());
    }
  }
  for (uint32_t id : ids) {
    if (!variables.contains(id)) {
      return MissingVariableStatus(id);
    }
  }
  std::vector<ProjectionVariable> projection_variables;
  projection_variables.reserve(ids.size());
  for (uint32_t id : ids) {
    projection_variables.push_back({id, variables.at(id)});
  }
  return projection_variables;
}

ProjectionRow ProjectionRowFromSolution(
    const sat::CpSolverResponse& response,
    const std::vector<ProjectionVariable>& projection_variables) {
  ProjectionRow row;
  row.values.reserve(projection_variables.size());
  for (const ProjectionVariable& projection_variable : projection_variables) {
    row.values.push_back(
        {projection_variable.id,
         sat::SolutionIntegerValue(response, projection_variable.variable)});
  }
  return row;
}

void AddForbiddenProjectionRow(
    const ProjectionRow& row,
    const std::vector<ProjectionVariable>& projection_variables,
    sat::CpModelBuilder* model) {
  std::vector<sat::IntVar> variables;
  variables.reserve(projection_variables.size());
  for (const ProjectionVariable& projection_variable : projection_variables) {
    variables.push_back(projection_variable.variable);
  }
  sat::TableConstraint forbidden = model->AddForbiddenAssignments(variables);
  std::vector<int64_t> values;
  values.reserve(row.values.size());
  for (const auto& [_variable_id, value] : row.values) {
    values.push_back(value);
  }
  forbidden.AddTuple(values);
}

SelectorCpSatResponse ResponseFromSolver(
    const sat::CpSolverResponse& solver_response,
    const std::set<ProjectionRow>& projection_rows, bool complete) {
  SelectorCpSatResponse response;
  response.set_solver_response_stats(sat::CpSolverResponseStats(solver_response));

  if (!projection_rows.empty()) {
    if (!complete) {
      response.set_status(SOLVER_STATUS_UNKNOWN);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic(
          "CP-SAT stopped before proving complete target support");
    } else {
      response.set_status(projection_rows.size() == 1 ? SOLVER_STATUS_SATISFIABLE
                                                      : SOLVER_STATUS_AMBIGUOUS);
      response.set_assignment_coverage(
          ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
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

  switch (solver_response.status()) {
    case sat::CpSolverStatus::INFEASIBLE:
      response.set_status(SOLVER_STATUS_UNSATISFIABLE);
      response.set_assignment_coverage(
          ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
      return response;
    case sat::CpSolverStatus::OPTIMAL:
    case sat::CpSolverStatus::FEASIBLE:
      response.set_status(SOLVER_STATUS_UNKNOWN);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic(
          "CP-SAT found a feasible solve but returned no projected rows");
      return response;
    case sat::CpSolverStatus::MODEL_INVALID:
      response.set_status(SOLVER_STATUS_INVALID);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic("CP-SAT reported MODEL_INVALID");
      return response;
    case sat::CpSolverStatus::UNKNOWN:
    default:
      response.set_status(SOLVER_STATUS_UNKNOWN);
      response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
      response.set_diagnostic("CP-SAT returned UNKNOWN");
      return response;
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
          AddLinearConstraints(request, *variables, model);
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

  absl::StatusOr<std::vector<ProjectionVariable>> projection_variables =
      ProjectionVariables(request, variables);
  if (!projection_variables.ok()) {
    return InvalidResponse(projection_variables.status());
  }

  std::set<ProjectionRow> projection_rows;
  sat::CpSolverResponse solver_response;
  for (;;) {
    sat::Model solver_model;
    sat::SatParameters parameters;
    parameters.set_num_search_workers(1);
    solver_model.Add(sat::NewSatParameters(parameters));

    const sat::CpModelProto model_proto = model.Build();
    solver_response = sat::SolveCpModel(model_proto, &solver_model);
    switch (solver_response.status()) {
      case sat::CpSolverStatus::OPTIMAL:
      case sat::CpSolverStatus::FEASIBLE: {
        ProjectionRow row =
            ProjectionRowFromSolution(solver_response, *projection_variables);
        projection_rows.insert(row);
        AddForbiddenProjectionRow(row, *projection_variables, &model);
        break;
      }
      case sat::CpSolverStatus::INFEASIBLE:
        return ResponseFromSolver(solver_response, projection_rows,
                                  /*complete=*/true);
      case sat::CpSolverStatus::MODEL_INVALID:
        return InvalidModelResponse(solver_response, model_proto);
      case sat::CpSolverStatus::UNKNOWN:
      default:
        return ResponseFromSolver(solver_response, projection_rows,
                                  /*complete=*/false);
    }
  }
}

}  // namespace ducktape::debundle::solver_backends::ortools_cpsat
