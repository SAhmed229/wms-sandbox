# A Reasonable WMS

Idea is to make a thinking (reasoning) model which can make suggestions/decisions on internal transfers which need to occur inside of a warehouse or between multiple warehouses. 

Essentially the idea is that AGV uptime / Forktruck operators are not at 100% utilization, and are 'in theory' never the limiting factor. This project does not include situations where AGV/Forktruck allocation as an problem as a consideration. 

Since they are not used 100% of the time, what are the productive tasks they can do while waiting? In theory the best thing to do is to prepare, and to do internal transfers? 

Well how do you decide on internal transfers? Staging the next product? It all depends on the scenario, and in the long run staging the next product may not be optimal as you could move several other products in an more efficient manner to be slightly closer, which would better the total average loading time. 

Eseentially the decision on internal transfers is relatively complicated, so we need some type of reasoning model or 'black box' to provide an output of what is proper based off of the current conditions. 

## Approaches 

### Multi Agentic Reinforcement Learning (MARL)
- Seems to be the best approach, as is computationally reasonable and can do exploration/thinking to adapt to different scenarios
- Pretraining can create different Agents, and can tune for different factors with a final arbiter picking those of highest 'value'
- Difficult to ascertain 'value' / figure out what truly is the most helpful (deciding on following/predicting trends or balancing)

### Simple Linear Regression
- Computationally heavy, won't be able to adapt to different conditions and large quantities of data
- Hard to deal with large varieties of factors

### Reasoning/Decision Tree Model
- Again, really difficult to make adaptable in anyways without each individual organization tuning as needed.

## Design Principles 
- Scalable (Each independent service needs to be able to scale up and down)
- Containerized (Each service needs to be limited in permissions and also be able to be manipulated)
- Adaptable (Should be able to exist in any production environment)
- Externalized (Setup to be a third party which can plug and play into any systems)

## Related Research
https://github.com/uoe-agents/task-assignment-robotic-warehouse
https://arxiv.org/pdf/2212.11498
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4701988****

## Closing Thoughts

Generally has not been done before and should be an interesting / unique problem to encounter.


